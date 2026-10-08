"""RouterBench-Lite — a verifiable-reward eval harness.

The only reason an internal benchmark is worth building is that the reward is
*verifiable*: SQL executes, JSON parses, a tool call matches a schema, a numeric
answer is within tolerance. Subjective "did the user accept it" is a weak signal
and is weighted as such, so every task here has a deterministic check.

Families map onto routing tasks so bench observations feed `routing_stats`
directly (an observation written under `sql_generation` is what the router reads
back when routing `sql_generation`):

    sql        -> sql_generation
    extraction -> extraction
    tool_call  -> agent_tools
    reasoning  -> hard_reasoning

`mi bench --self-test` feeds each task's gold answer through its own verifier and
must report 100% — that is the guarantee that the verifiers themselves are sound,
independent of any model's behaviour.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from .execute import CallResult, Runner
from .fetch import utcnow
from .store import Store

# --------------------------------------------------------------------------- #
# families
# --------------------------------------------------------------------------- #

FAMILY_ROUTE_TASK = {
    "sql": "sql_generation",
    "extraction": "extraction",
    "tool_call": "agent_tools",
    "reasoning": "hard_reasoning",
}
FAMILIES = tuple(FAMILY_ROUTE_TASK)


@dataclass(slots=True)
class BenchTask:
    task_id: str
    family: str
    prompt: str
    check: dict
    tokens_in: int
    tokens_out: int
    #: Hand-labelled intrinsic difficulty. This is the graded axis the effort
    #: calibration needs: without it a policy can only be scored on the average, and
    #: an average is exactly what hides "great at easy, bad at hard".
    difficulty: str = "medium"


# --------------------------------------------------------------------------- #
# gold answers + shared verifier primitives
# --------------------------------------------------------------------------- #


def _extract_fenced(text: str, lang: str) -> str:
    m = re.search(rf"```(?:{lang})?\s*(.*?)```", text, re.S | re.I)
    return m.group(1).strip() if m else text.strip()


def _extract_json(text: str) -> dict | None:
    t = _extract_fenced(text, "json")
    try:
        return json.loads(t)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num_eq(a, b, tol: float = 1e-6) -> bool:
    fa, fb = _num(a), _num(b)
    if fa is None or fb is None:
        return False
    return abs(fa - fb) <= tol * max(1.0, abs(fb))


def _str_eq(a, b) -> bool:
    return str(a).strip().lower() == str(b).strip().lower()


def _val_eq(got, want) -> bool:
    if isinstance(want, bool):
        return bool(got) is want
    if isinstance(want, (int, float)) and not isinstance(want, bool):
        return _num_eq(got, want)
    return _str_eq(got, want)


def _sql_extract(text: str) -> str:
    t = _extract_fenced(text, "sql")
    i = t.lower().find("select")
    if i > 0:
        t = t[i:]
    return t.strip().rstrip(";")


def _sql_rows(sql: str, schema: list[str], data: list[str]) -> list[tuple] | None:
    try:
        con = sqlite3.connect(":memory:")
        con.executescript(";\n".join(schema + data) + ";")
        cur = con.execute(sql)
        return cur.fetchall()
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def _norm_row_val(v):
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, int):
        return v
    return str(v)


def _rows_equal(a: list[tuple], b: list[tuple]) -> bool:
    if len(a) != len(b):
        return False
    na = sorted([tuple(_norm_row_val(x) for x in r) for r in a])
    nb = sorted([tuple(_norm_row_val(x) for x in r) for r in b])
    return na == nb


def verify(task: BenchTask, completion: str) -> tuple[bool, float]:
    """Deterministic reward. (ok, score) — score is 1.0 on success, 0.0 else."""
    if task.family == "sql":
        return _verify_sql(task.check, completion)
    if task.family == "extraction":
        return _verify_extraction(task.check, completion)
    if task.family == "tool_call":
        return _verify_tool(task.check, completion)
    if task.family == "reasoning":
        return _verify_reasoning(task.check, completion)
    return False, 0.0


def _verify_sql(check: dict, completion: str) -> tuple[bool, float]:
    ref = _sql_rows(check["reference_sql"], check["schema"], check["data"])
    if ref is None:
        return False, 0.0
    got = _sql_rows(_sql_extract(completion), check["schema"], check["data"])
    if got is None:
        return False, 0.0
    ok = _rows_equal(got, ref)
    return ok, 1.0 if ok else 0.0


def _verify_extraction(check: dict, completion: str) -> tuple[bool, float]:
    data = _extract_json(completion)
    if not isinstance(data, dict):
        return False, 0.0
    expected = check["expected"]
    for k, want in expected.items():
        if k not in data or not _val_eq(data[k], want):
            return False, 0.0
    return True, 1.0


def _tool_name_clean(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _verify_tool(check: dict, completion: str) -> tuple[bool, float]:
    data = _extract_json(completion)
    if not isinstance(data, dict):
        return False, 0.0
    name = data.get("name") or data.get("tool")
    if isinstance(data.get("function"), dict):
        name = name or data["function"].get("name")
    args = data.get("arguments") or data.get("args") or data.get("parameters")
    if isinstance(data.get("function"), dict) and not args:
        args = data["function"].get("arguments")
    if name is None:
        # A bare object is accepted if it already carries the expected args.
        name, args = check["expected_name"], data
    if not _str_eq(name, check["expected_name"]):
        return False, 0.0
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return False, 0.0
    if not isinstance(args, dict):
        return False, 0.0
    for k, want in check["expected_args"].items():
        if k not in args or not _val_eq(args[k], want):
            return False, 0.0
    return True, 1.0


def _verify_reasoning(check: dict, completion: str) -> tuple[bool, float]:
    nums = re.findall(r"-?\d+(?:\.\d+)?", completion)
    if not nums:
        return False, 0.0
    got = float(nums[-1])
    want = check["answer"]
    tol = check.get("tolerance", 1e-2)
    return abs(got - want) <= tol, 1.0 if abs(got - want) <= tol else 0.0


def gold_answer(task: BenchTask) -> str:
    """A completion that must verify true for this task. The benchmark self-check."""
    if task.family == "sql":
        return f"```sql\n{task.check['reference_sql']}\n```"
    if task.family == "extraction":
        return json.dumps(task.check["expected"], ensure_ascii=False)
    if task.family == "tool_call":
        return json.dumps({"name": task.check["expected_name"],
                           "arguments": task.check["expected_args"]})
    if task.family == "reasoning":
        return str(task.check["answer"])
    return ""


# --------------------------------------------------------------------------- #
# task corpus
# --------------------------------------------------------------------------- #

_STORE_SCHEMA = [
    "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, category TEXT, price REAL)",
    "CREATE TABLE orders (id INTEGER PRIMARY KEY, product_id INTEGER REFERENCES products(id),"
    " qty INTEGER, order_date TEXT)",
]
_STORE_DATA = [
    "INSERT INTO products VALUES (1,'Laptop','electronics',1200.0)",
    "INSERT INTO products VALUES (2,'Mouse','electronics',25.0)",
    "INSERT INTO products VALUES (3,'Desk','furniture',350.0)",
    "INSERT INTO products VALUES (4,'Chair','furniture',180.0)",
    "INSERT INTO products VALUES (5,'Novel','books',15.0)",
    "INSERT INTO products VALUES (6,'Textbook','books',80.0)",
    "INSERT INTO products VALUES (7,'Headphones','electronics',90.0)",
    "INSERT INTO orders VALUES (1,1,2,'2026-01-05')",
    "INSERT INTO orders VALUES (2,2,5,'2026-01-06')",
    "INSERT INTO orders VALUES (3,4,1,'2026-01-10')",
    "INSERT INTO orders VALUES (4,1,1,'2026-02-02')",
    "INSERT INTO orders VALUES (5,5,3,'2026-02-14')",
    "INSERT INTO orders VALUES (6,3,2,'2026-03-01')",
    "INSERT INTO orders VALUES (7,2,4,'2026-03-03')",
]

_LIB_SCHEMA = [
    "CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author TEXT, year INTEGER)",
    "CREATE TABLE loans (id INTEGER PRIMARY KEY, book_id INTEGER REFERENCES books(id),"
    " borrower TEXT, loan_date TEXT, return_date TEXT)",
]
_LIB_DATA = [
    "INSERT INTO books VALUES (1,'Dune','Herbert',1965)",
    "INSERT INTO books VALUES (2,'Neuromancer','Gibson',1984)",
    "INSERT INTO books VALUES (3,'Foundation','Asimov',1951)",
    "INSERT INTO books VALUES (4,'Hyperion','Simmons',1989)",
    "INSERT INTO books VALUES (5,'The Martian','Weir',2011)",
    "INSERT INTO books VALUES (6,'Dune Messiah','Herbert',1969)",
    "INSERT INTO loans VALUES (1,1,'alice','2026-01-02','2026-01-09')",
    "INSERT INTO loans VALUES (2,2,'bob','2026-01-05',NULL)",
    "INSERT INTO loans VALUES (3,3,'alice','2026-01-12','2026-01-20')",
    "INSERT INTO loans VALUES (4,1,'alice','2026-02-01',NULL)",
    "INSERT INTO loans VALUES (5,4,'bob','2026-02-10','2026-02-17')",
]

_EMP_SCHEMA = [
    "CREATE TABLE dept (id INTEGER PRIMARY KEY, name TEXT)",
    "CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT, dept_id INTEGER REFERENCES dept(id),"
    " salary INTEGER, manager_id INTEGER REFERENCES emp(id))",
]
_EMP_DATA = [
    "INSERT INTO dept VALUES (1,'Engineering')",
    "INSERT INTO dept VALUES (2,'Sales')",
    "INSERT INTO dept VALUES (3,'HR')",
    "INSERT INTO emp VALUES (1,'Ada',1,150000,NULL)",
    "INSERT INTO emp VALUES (2,'Grace',1,140000,1)",
    "INSERT INTO emp VALUES (3,'Alan',1,120000,2)",
    "INSERT INTO emp VALUES (4,'Bob',2,90000,NULL)",
    "INSERT INTO emp VALUES (5,'Carol',2,85000,4)",
    "INSERT INTO emp VALUES (6,'Dan',3,70000,NULL)",
]


def _sql_task(n: int, question: str, reference_sql: str, schema: list[str], data: list[str]) -> BenchTask:
    ddl = "\n".join(schema + data)
    prompt = (f"You have a SQLite database with this schema and data:\n\n{ddl}\n\n"
              f"Write ONE SQL query that answers: {question}\n"
              f"Return only the SQL, no explanation, no markdown fences.")
    tid = f"sql-{n:02d}"
    return BenchTask(tid, "sql", prompt,
                     {"schema": schema, "data": data, "reference_sql": reference_sql},
                     tokens_in=700, tokens_out=120,
                     difficulty=DIFFICULTY_BY_ID.get(tid, "medium"))


_SQL = [
    ("List the names of every product in the electronics category, in alphabetical order.",
     "SELECT name FROM products WHERE category='electronics' ORDER BY name",
     _STORE_SCHEMA, _STORE_DATA),
    ("For each product, show its name and the total quantity ordered, most ordered first.",
     "SELECT p.name, COALESCE(SUM(o.qty),0) AS total FROM products p"
     " LEFT JOIN orders o ON o.product_id=p.id GROUP BY p.id ORDER BY total DESC, p.name",
     _STORE_SCHEMA, _STORE_DATA),
    ("Which single product generated the highest total revenue (price * quantity)? Return its name.",
     "SELECT p.name FROM products p JOIN orders o ON o.product_id=p.id"
     " GROUP BY p.id ORDER BY SUM(o.qty*p.price) DESC LIMIT 1",
     _STORE_SCHEMA, _STORE_DATA),
    ("How many orders were placed after 2026-02-01?",
     "SELECT COUNT(*) FROM orders WHERE order_date > '2026-02-01'",
     _STORE_SCHEMA, _STORE_DATA),
    ("What is the average price of products in the books category?",
     "SELECT AVG(price) FROM products WHERE category='books'",
     _STORE_SCHEMA, _STORE_DATA),
    ("List the names of products that have never been ordered.",
     "SELECT p.name FROM products p LEFT JOIN orders o ON o.product_id=p.id"
     " WHERE o.id IS NULL ORDER BY p.name",
     _STORE_SCHEMA, _STORE_DATA),
    ("What is the cheapest product price in each category? Show category and price.",
     "SELECT category, MIN(price) FROM products GROUP BY category ORDER BY category",
     _STORE_SCHEMA, _STORE_DATA),
    ("What fraction of all orders are for products in the electronics category?",
     "SELECT (SELECT COUNT(*) FROM orders o JOIN products p ON p.id=o.product_id"
     " WHERE p.category='electronics') * 1.0 / COUNT(*) FROM orders",
     _STORE_SCHEMA, _STORE_DATA),
    ("Show each category and its total quantity ordered, highest first, including categories"
     " with zero orders.",
     "SELECT p.category, COALESCE(SUM(o.qty),0) AS total FROM products p"
     " LEFT JOIN orders o ON o.product_id=p.id GROUP BY p.category"
     " ORDER BY total DESC, p.category",
     _STORE_SCHEMA, _STORE_DATA),
    ("Which products are priced above the average price of all products? List their names.",
     "SELECT name FROM products WHERE price > (SELECT AVG(price) FROM products) ORDER BY name",
     _STORE_SCHEMA, _STORE_DATA),
    ("How many books are currently on loan (return_date is null)?",
     "SELECT COUNT(*) FROM loans WHERE return_date IS NULL",
     _LIB_SCHEMA, _LIB_DATA),
    ("Which borrower has borrowed the most books?",
     "SELECT borrower FROM loans GROUP BY borrower ORDER BY COUNT(*) DESC LIMIT 1",
     _LIB_SCHEMA, _LIB_DATA),
    ("List the titles of books published before 1980, ordered by year.",
     "SELECT title FROM books WHERE year < 1980 ORDER BY year",
     _LIB_SCHEMA, _LIB_DATA),
    ("Which books have never been borrowed? List their titles.",
     "SELECT b.title FROM books b LEFT JOIN loans l ON l.book_id=b.id"
     " WHERE l.id IS NULL ORDER BY b.title",
     _LIB_SCHEMA, _LIB_DATA),
    ("How many loans were made in January 2026?",
     "SELECT COUNT(*) FROM loans WHERE loan_date >= '2026-01-01' AND loan_date < '2026-02-01'",
     _LIB_SCHEMA, _LIB_DATA),
    ("Show each author and the number of books they have in the catalogue, most books first.",
     "SELECT author, COUNT(*) FROM books GROUP BY author ORDER BY COUNT(*) DESC, author",
     _LIB_SCHEMA, _LIB_DATA),
    ("List the names and salaries of employees in Engineering, ordered by salary descending.",
     "SELECT e.name, e.salary FROM emp e JOIN dept d ON d.id=e.dept_id"
     " WHERE d.name='Engineering' ORDER BY e.salary DESC",
     _EMP_SCHEMA, _EMP_DATA),
    ("What is the total salary paid by each department? Show department name and total,"
     " highest first.",
     "SELECT d.name, SUM(e.salary) FROM emp e JOIN dept d ON d.id=e.dept_id"
     " GROUP BY d.id ORDER BY SUM(e.salary) DESC",
     _EMP_SCHEMA, _EMP_DATA),
    ("Which employees earn more than their manager? List their names.",
     "SELECT e.name FROM emp e JOIN emp m ON m.id=e.manager_id WHERE e.salary > m.salary",
     _EMP_SCHEMA, _EMP_DATA),
    ("Who is the highest-paid employee in each department? Show department name and employee name.",
     "SELECT d.name, e.name FROM emp e JOIN dept d ON d.id=e.dept_id"
     " WHERE e.salary = (SELECT MAX(e2.salary) FROM emp e2 WHERE e2.dept_id=e.dept_id)"
     " ORDER BY d.name",
     _EMP_SCHEMA, _EMP_DATA),
]


def _extraction_task(n: int, prompt: str, expected: dict) -> BenchTask:
    fields = ", ".join(expected)
    prompt = (prompt + "\n\nExtract these fields as a JSON object with these exact keys: "
              + fields + ". Return only JSON.")
    tid = f"extract-{n:02d}"
    return BenchTask(tid, "extraction", prompt,
                     {"expected": expected}, tokens_in=400, tokens_out=150,
                     difficulty=DIFFICULTY_BY_ID.get(tid, "medium"))


_EXTRACTION = [
    ("Order received: customer John Smith, email john.smith@example.com, ordered 3 units of"
     " the Pro Widget at $49.99 each.",
     {"customer": "John Smith", "email": "john.smith@example.com", "quantity": 3,
      "unit_price": 49.99, "product": "Pro Widget"}),
    ("Meeting scheduled for the design review. First attendee: Priya Patel. Room: Atlas."
     " Start time: 10:30.",
     {"first_attendee": "Priya Patel", "room": "Atlas", "start_time": "10:30"}),
    ("Shipment SHIP-88-2241 left the warehouse in Rotterdam, Netherlands, weighing 1,240 kg,"
     " destined for Tokyo, Japan.",
     {"shipment_id": "SHIP-88-2241", "origin": "Rotterdam", "destination": "Tokyo",
      "weight_kg": 1240}),
    ("The candidate scored 92 on the written test and 87 on the interview. Name: Nadia Okafor.",
     {"name": "Nadia Okafor", "written_score": 92, "interview_score": 87}),
    ("Server incident INC-5521: CPU spiked to 98% at 14:22 UTC; service api-gateway affected;"
     " 214 requests failed.",
     {"incident_id": "INC-5521", "cpu_percent": 98, "service": "api-gateway",
      "failed_requests": 214}),
    ("Subscription plan 'Growth' is $249 per month, billed annually. Seats included: 25."
     " Discount code: SCALE20.",
     {"plan": "Growth", "monthly_price": 249, "seats": 25, "discount_code": "SCALE20"}),
    ("Flight BA-298 from London Heathrow to New York JFK, departure 09:15, gate B22.",
     {"flight": "BA-298", "origin": "London Heathrow", "destination": "New York JFK",
      "gate": "B22"}),
    ("The patient's blood pressure was 118/76 and resting heart rate 64 bpm. Patient ID: P-004812.",
     {"patient_id": "P-004812", "systolic": 118, "diastolic": 76, "heart_rate_bpm": 64}),
    ("Recipe: 250g flour, 2 eggs, 120ml milk, 1 tsp salt, bake at 180C for 25 minutes.",
     {"flour_g": 250, "eggs": 2, "milk_ml": 120, "bake_temp_c": 180, "bake_min": 25}),
    ("Car listing: 2021 Tesla Model 3, 34,000 miles, asking price $29,500,"
     " VIN 5YJ3E1EA7MF000000, located in Austin, TX.",
     {"year": 2021, "make": "Tesla", "model": "Model 3", "miles": 34000, "price": 29500,
      "vin": "5YJ3E1EA7MF000000", "location": "Austin"}),
]


def _tool_task(n: int, request: str, tools: list[dict], name: str, args: dict) -> BenchTask:
    prompt = ("Available tools (JSON):\n" + json.dumps(tools, ensure_ascii=False)
              + "\n\nRespond with exactly one JSON object of the form"
              ' {"name": "<tool>", "arguments": {<params>}}.\n\nUser request: ' + request)
    tid = f"tool-{n:02d}"
    return BenchTask(tid, "tool_call", prompt,
                     {"tools": tools, "expected_name": name, "expected_args": args},
                     tokens_in=400, tokens_out=120,
                     difficulty=DIFFICULTY_BY_ID.get(tid, "medium"))


_TOOLS = [
    ("What is the weather in Lisbon right now?",
     [{"name": "get_weather", "parameters": {"city": "string"}}],
     "get_weather", {"city": "Lisbon"}),
    ("Search for 'wilson score interval'.",
     [{"name": "search", "parameters": {"query": "string"}}],
     "search", {"query": "wilson score interval"}),
    ("Email dana@example.com about the delayed shipment.",
     [{"name": "send_email", "parameters": {"to": "string", "subject": "string",
                                            "body": "string"}}],
     "send_email", {"to": "dana@example.com", "subject": "Delayed shipment"}),
    ("Schedule 'Team standup' at 09:00 for 15 minutes.",
     [{"name": "create_event", "parameters": {"title": "string", "start": "string",
                                              "duration_min": "integer"}}],
     "create_event", {"title": "Team standup", "start": "09:00", "duration_min": 15}),
    ("Compute 45 * 12.",
     [{"name": "calc", "parameters": {"expression": "string"}}],
     "calc", {"expression": "45 * 12"}),
    ("Move $120.50 from checking to savings.",
     [{"name": "transfer", "parameters": {"from": "string", "to": "string",
                                          "amount": "number"}}],
     "transfer", {"from": "checking", "to": "savings", "amount": 120.5}),
]


def _reasoning_task(n: int, prompt: str, answer: float, tolerance: float = 1e-2) -> BenchTask:
    tid = f"reason-{n:02d}"
    return BenchTask(tid, "reasoning", prompt,
                     {"answer": answer, "tolerance": tolerance},
                     tokens_in=300, tokens_out=60,
                     difficulty=DIFFICULTY_BY_ID.get(tid, "medium"))


_REASONING = [
    ("A train travels 240 km in 3 hours. What is its average speed in km/h? Answer with a number.", 80.0),
    ("If 5 machines make 5 widgets in 5 minutes, how many widgets do 10 machines make in"
     " 10 minutes? Answer with a number.", 20.0),
    ("A shirt costs $40 after a 20% discount. What was the original price in dollars?"
     " Answer with a number.", 50.0),
    ("Solve for x: 3x + 7 = 22. Answer with a number.", 5.0),
    ("What is 15% of 240? Answer with a number.", 36.0),
    ("A car uses 8 litres per 100 km. How many litres for 350 km? Answer with a number.", 28.0),
]


def build_tasks() -> list[BenchTask]:
    tasks: list[BenchTask] = []
    for i, (q, ref, schema, data) in enumerate(_SQL, start=1):
        tasks.append(_sql_task(i, q, ref, schema, data))
    for i, (prompt, expected) in enumerate(_EXTRACTION, start=1):
        tasks.append(_extraction_task(i, prompt, expected))
    for i, (req, tools, name, args) in enumerate(_TOOLS, start=1):
        tasks.append(_tool_task(i, req, tools, name, args))
    for i, (prompt, answer) in enumerate(_REASONING, start=1):
        tasks.append(_reasoning_task(i, prompt, answer))
    return tasks



#: Hand-labelled difficulty per task id, the input to `calibrate_floor_delta`.
#:
#: The rule, so it can be argued with rather than trusted: `low` is one obvious
#: step (single table, one filter, one arithmetic operation); `medium` needs a join,
#: a group, or a second step; `high` needs a subquery, a self-join, a ratio of
#: aggregates, or an anti-join. Small and hand-made on purpose — the same caveat as
#: `complexity.CALIBRATION_SET` — and it is an *input* to calibration, not a claim
#: about any model.
DIFFICULTY_BY_ID: dict[str, str] = {
    "sql-01": "low",     "sql-02": "medium", "sql-03": "medium", "sql-04": "low",
    "sql-05": "low",     "sql-06": "high",   "sql-07": "medium", "sql-08": "high",
    "sql-09": "medium",  "sql-10": "high",   "sql-11": "low",    "sql-12": "medium",
    "sql-13": "low",     "sql-14": "high",   "sql-15": "low",    "sql-16": "medium",
    "sql-17": "low",     "sql-18": "medium", "sql-19": "high",   "sql-20": "high",
    "extract-01": "low", "extract-02": "low", "extract-03": "medium", "extract-04": "low",
    "extract-05": "medium", "extract-06": "low", "extract-07": "medium", "extract-08": "low",
    "extract-09": "low", "extract-10": "medium",
    "tool-01": "low", "tool-02": "low", "tool-03": "medium",
    "tool-04": "medium", "tool-05": "low", "tool-06": "medium",
    # Within `hard_reasoning`, most of these are one-step: the task profile is strict,
    # the prompts are not. That gap is precisely what the effort axis is for.
    "reason-01": "low", "reason-02": "medium", "reason-03": "medium",
    "reason-04": "low", "reason-05": "low", "reason-06": "low",
}

#: `floor_delta` values the calibration sweeps. 0.0 is the flat floor — the
#: control the delta has to beat.
DEFAULT_FLOOR_DELTA_GRID: tuple[float, ...] = (0.0, 0.04, 0.08, 0.12, 0.16)

TASKS = build_tasks()
TASKS_BY_FAMILY = {f: [t for t in TASKS if t.family == f] for f in FAMILIES}


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class BenchReport:
    family: str
    deploy_id: str
    total: int
    ok: int
    failed: int
    errors: int
    total_cost_usd: float | None
    rows: list[dict] = field(default_factory=list)


def _deploy_prices(store: Store, deploy_id: str) -> tuple[float | None, float | None]:
    row = store.deployment_prices(deploy_id)
    if row is None:
        return None, None
    return row["price_in"], row["price_out"]


def _cost_usd(store: Store, deploy_id: str, tin: int, tout: int) -> float | None:
    pin, pout = _deploy_prices(store, deploy_id)
    if pin is None or pout is None:
        return None
    return (pin * tin + pout * tout) / 1_000_000


def run_bench(
    store: Store,
    family: str,
    deploy_id: str,
    runner,
    *,
    write: bool = True,
    limit: int | None = None,
) -> BenchReport:
    tasks = TASKS_BY_FAMILY[family]
    route_task = FAMILY_ROUTE_TASK[family]
    rows: list[dict] = []
    ok = failed = errors = 0
    total_cost = 0.0

    for t in tasks[:limit]:
        res: CallResult = runner(deploy_id, [{"role": "user", "content": t.prompt}])
        verified, score = False, 0.0
        if res.ok:
            verified, score = verify(t, res.text)
        error_class = res.error_class if not res.ok else (None if verified else "bad_output")
        if res.ok and verified:
            ok += 1
        elif not res.ok:
            errors += 1
        else:
            failed += 1

        tin = res.tokens_in or t.tokens_in
        tout = res.tokens_out or t.tokens_out
        cost = _cost_usd(store, deploy_id, tin, tout) if res.ok else None
        if cost is not None:
            total_cost += cost
        rows.append({"task": t.task_id, "ok": res.ok and verified, "error": error_class,
                     "score": score, "latency_ms": res.latency_ms, "cost_usd": cost})

        if write:
            # A bench call consumes the same quota a production call would, so a
            # free arm's headroom drains during evaluation too.
            store.record_usage(deploy_id)
            if res.error_class == "429":
                store.exhaust(deploy_id, window="minute")
            store.observe(
                deploy_id, route_task, ok=res.ok and verified, ts=utcnow(),
                error_class=error_class, latency_ms=res.latency_ms,
                tokens_in=tin, tokens_out=tout, cost_usd=cost,
                signal_kind="verified", signal_value=score,
            )

    if write:
        store.commit()
    return BenchReport(family, deploy_id, len(rows), ok, failed, errors,
                       total_cost if ok or failed else None, rows)


def self_test() -> tuple[int, int]:
    """Feed every gold answer through its verifier; must be (N, 0)."""
    passed = failed = 0
    for t in TASKS:
        ok, _ = verify(t, gold_answer(t))
        if ok:
            passed += 1
        else:
            failed += 1
    return passed, failed


# --------------------------------------------------------------------------- #
# routed bench: score the *decision*, graded by difficulty
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RoutedRow:
    task: str
    difficulty: str
    deploy_id: str
    ok: bool
    error: str | None
    cost_usd: float | None
    #: Whether the classifier put this prompt in the `needs_reasoning` band. Only
    #: those prompts are affected by `floor_delta` at all — on the rest the sweep is
    #: measuring noise, and identical rows across the grid mean *inert*, not *flat*.
    needs_reasoning: bool = False


@dataclass(slots=True)
class RoutedReport:
    family: str
    floor_delta: float
    rows: list[RoutedRow] = field(default_factory=list)

    def by_difficulty(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.rows:
            agg = out.setdefault(r.difficulty, {"n": 0, "ok": 0, "cost_usd": 0.0})
            agg["n"] += 1
            agg["ok"] += int(r.ok)
            agg["cost_usd"] += r.cost_usd or 0.0
        for agg in out.values():
            agg["success_rate"] = round(agg["ok"] / agg["n"], 4) if agg["n"] else 0.0
            agg["cost_per_success"] = (round(agg["cost_usd"] / agg["ok"], 6)
                                       if agg["ok"] else None)
            agg["cost_usd"] = round(agg["cost_usd"], 6)
        return out

    def summary(self) -> dict:
        by = self.by_difficulty()
        n = sum(a["n"] for a in by.values())
        ok = sum(a["ok"] for a in by.values())
        cost = sum(a["cost_usd"] for a in by.values())
        return {"family": self.family, "floor_delta": self.floor_delta,
                "n": n, "ok": ok,
                "success_rate": round(ok / n, 4) if n else 0.0,
                "cost_usd": round(cost, 6),
                "cost_per_success": round(cost / ok, 6) if ok else None,
                "by_difficulty": by,
                # How much of this set the delta can even act on. Zero means the
                # recommendation below is meaningless however tidy it looks.
                "exercises_delta": sum(1 for r in self.rows if r.needs_reasoning)}


def run_routed_bench(store: Store, family: str, runner, *, policy, tasks_by_name,
                     floor_delta: float, limit: int | None = None,
                     write: bool = False, classify=None) -> RoutedReport:
    """Route every graded task and score the *decision*, not one deployment.

    `run_bench` measures how a deployment performs; this measures how the router
    performs at a given `floor_delta`, split by the task's difficulty. That split is
    the only way to see whether the delta helps the hard prompts without taxing the
    easy ones — which is the entire claim under test.
    """
    from .complexity import adapt_task_for_complexity, classify_complexity
    from .router import route

    classify = classify or classify_complexity
    profile = tasks_by_name[FAMILY_ROUTE_TASK[family]]
    report = RoutedReport(family=family, floor_delta=floor_delta)

    for t in TASKS_BY_FAMILY[family][:limit]:
        cx = classify(t.prompt)
        adapted = adapt_task_for_complexity(profile, cx, floor_delta=floor_delta)
        dec = route(store, adapted, policy, mode="auto", effort=cx.level)
        if not dec.chosen:
            report.rows.append(RoutedRow(t.task_id, t.difficulty, "", False,
                                         "no_candidates", None))
            continue
        deploy_id = dec.chosen[0].deploy_id
        res: CallResult = runner(deploy_id, [{"role": "user", "content": t.prompt}])
        verified, _score = verify(t, res.text) if res.ok else (False, 0.0)
        ok = bool(res.ok and verified)
        error = res.error_class if not res.ok else (None if verified else "bad_output")
        tin = res.tokens_in or t.tokens_in
        tout = res.tokens_out or t.tokens_out
        cost = _cost_usd(store, deploy_id, tin, tout) if res.ok else None
        report.rows.append(RoutedRow(t.task_id, t.difficulty, deploy_id, ok, error, cost,
                                     needs_reasoning=bool(cx.needs_reasoning)))

        if write:
            # A routed bench call burns real quota, and the effort tag is what feeds
            # the very loop this calibrates — so it is recorded like production.
            store.record_usage(deploy_id)
            if res.error_class == "429":
                store.exhaust(deploy_id, window="minute")
            store.observe(deploy_id, FAMILY_ROUTE_TASK[family], ok=ok, ts=utcnow(),
                          error_class=error, latency_ms=res.latency_ms,
                          tokens_in=tin, tokens_out=tout, cost_usd=cost,
                          signal_kind="verified", effort=cx.level)
    if write:
        store.commit()
    return report


def calibrate_floor_delta(store: Store, family: str, runner, *, policy, tasks_by_name,
                          grid: tuple[float, ...] | None = None,
                          limit: int | None = None, write: bool = False,
                          tolerance: float = 0.0, classify=None) -> dict:
    """Sweep `floor_delta`; recommend the cheapest that is no worse.

    The comparison is deliberately not "which delta wins most" — it is "which delta
    reaches the same success for less". A delta that buys hard-prompt accuracy by
    paying more on every easy prompt is not an improvement, and the per-difficulty
    split is what makes that visible.
    """
    deltas = grid or DEFAULT_FLOOR_DELTA_GRID
    runs = [run_routed_bench(store, family, runner, policy=policy,
                             tasks_by_name=tasks_by_name, floor_delta=d,
                             limit=limit, write=write, classify=classify).summary()
            for d in deltas]

    ranked = [r for r in runs if r["cost_per_success"] is not None]
    if not ranked:
        return {"family": family, "grid": list(deltas), "runs": runs,
                "recommended": None, "exercises_delta": 0}
    best = max(r["success_rate"] for r in ranked)
    eligible = [r for r in ranked if r["success_rate"] >= best - tolerance]
    recommended = min(eligible, key=lambda r: (r["cost_per_success"], r["floor_delta"]))
    return {"family": family, "grid": list(deltas), "runs": runs,
            "recommended": recommended,
            "exercises_delta": max((r.get("exercises_delta", 0) for r in runs), default=0)}
