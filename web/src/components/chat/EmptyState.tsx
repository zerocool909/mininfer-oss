import { Terminal, Cpu, Scale, MessageSquareCode } from 'lucide-react'

interface EmptyStateProps {
  onSelectPrompt: (prompt: string, task?: string, compare?: boolean) => void
}

const STARTER_PROMPTS = [
  {
    icon: MessageSquareCode,
    title: 'Write code',
    desc: 'A debounced TypeScript function with generics',
    task: 'code_edit',
    prompt: 'Write a clean TypeScript debounce function with proper generic typing and cancellation.',
  },
  {
    icon: Cpu,
    title: 'Reason it through',
    desc: 'Optimistic vs pessimistic concurrency control',
    task: 'hard_reasoning',
    prompt: 'Compare optimistic vs pessimistic concurrency control in distributed databases. When should I choose each?',
  },
  {
    icon: Scale,
    title: 'Compare two models',
    desc: 'Run the router’s top two head-to-head',
    task: 'auto',
    compare: true,
    prompt: 'Explain the difference between process memory and thread stack with a vivid analogy.',
  },
  {
    icon: Terminal,
    title: 'Free-tier chat',
    desc: 'Answer with zero-cost routing priority',
    task: 'general_chat',
    prompt: 'What are the top 3 principles of writing maintainable open source libraries?',
  },
]

export function EmptyState({ onSelectPrompt }: EmptyStateProps) {
  return (
    <div className="mx-auto flex w-full max-w-xl flex-col items-center justify-center px-4 py-8 text-center animate-fade-up">
      <div className="mb-6 flex items-center justify-center">
        <img
          src="/mininfer-logo.svg"
          alt="MinInfer"
          className="h-24 w-auto max-w-[380px] object-contain drop-shadow-md transition-transform hover:scale-[1.02]"
        />
      </div>
      <p className="mt-2 max-w-sm text-[13.5px] leading-6 text-muted-foreground">
        Every request goes to the cheapest model that can do it. Free first — pay only when free
        isn’t good enough.
      </p>

      <div className="mt-9 grid w-full grid-cols-1 gap-2.5 text-left sm:grid-cols-2">
        {STARTER_PROMPTS.map((item, i) => {
          const Icon = item.icon
          return (
            <button
              key={i}
              onClick={() => onSelectPrompt(item.prompt, item.task, item.compare)}
              className="group flex flex-col rounded-xl border border-border bg-card p-3.5 text-left shadow-soft transition-all hover:-translate-y-0.5 hover:border-brand/50 hover:shadow-glow-sm"
            >
              <div className="flex items-center gap-2">
                <Icon className="h-4 w-4 text-muted-foreground transition-colors group-hover:text-brand" strokeWidth={1.9} />
                <span className="text-[13px] font-medium text-foreground">{item.title}</span>
                {item.compare && (
                  <span className="ml-auto rounded-full bg-brand/10 px-1.5 py-0.5 text-[10px] font-medium text-brand">
                    2 models
                  </span>
                )}
              </div>
              <p className="mt-1.5 line-clamp-2 text-[12px] leading-5 text-muted-foreground">
                {item.desc}
              </p>
            </button>
          )
        })}
      </div>
    </div>
  )
}
