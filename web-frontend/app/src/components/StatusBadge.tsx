import { cn } from '@/lib/utils'

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error'

interface StatusBadgeProps {
  status: ConnectionStatus
  roomName?: string
}

const dotStyles: Record<ConnectionStatus, string> = {
  disconnected: 'bg-neutral-500',
  connecting: 'bg-yellow-500 animate-pulse',
  connected: 'bg-green-500',
  error: 'bg-red-500',
}

const labels: Record<ConnectionStatus, string> = {
  disconnected: 'Disconnected',
  connecting: 'Connecting...',
  connected: 'Connected',
  error: 'Error',
}

export default function StatusBadge({ status, roomName }: StatusBadgeProps) {
  const label = status === 'connected' && roomName
    ? `Connected to ${roomName}`
    : labels[status]

  return (
    <div className="inline-flex items-center gap-1 sm:gap-1.5 px-0 py-0.5 text-[10px] sm:text-xs text-neutral-500 mb-1 sm:mb-2">
      <span className={cn('w-1.5 h-1.5 rounded-full', dotStyles[status])} />
      <span className="truncate max-w-[200px]">{label}</span>
    </div>
  )
}
