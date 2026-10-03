import Chat02 from '@/components/ui/chat-02'

export interface TestTarget {
  deployId: string
  task: string
}

/**
 * The Playground is the chat surface: it talks to the same
 * `/v1/chat/completions` endpoint any OpenAI client uses, and surfaces what the
 * router decided (model, cost, leaderboards) alongside the answer.
 *
 * `testTarget` is how the Overview's row-level "Test" button hands a specific
 * deployment to the chat — pinning it for the next request instead of dumping
 * the user on an empty composer.
 */
export function Playground({
  testTarget,
  onTestConsumed,
}: {
  testTarget?: TestTarget | null
  onTestConsumed?: () => void
}) {
  return <Chat02 testTarget={testTarget} onTestConsumed={onTestConsumed} />
}
