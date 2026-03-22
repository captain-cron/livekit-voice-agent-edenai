interface AgentStatusProps {
  text: string
}

export default function AgentStatus({ text }: AgentStatusProps) {
  if (!text) return null
  return <p className="mt-1 sm:mt-2 text-[11px] sm:text-sm text-neutral-500">{text}</p>
}
