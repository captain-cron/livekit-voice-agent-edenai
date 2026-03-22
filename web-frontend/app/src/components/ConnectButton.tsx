import type { ConnectionStatus } from './StatusBadge'

interface ConnectButtonProps {
  status: ConnectionStatus
  onConnect: () => void
  onDisconnect: () => void
}

export default function ConnectButton({ status, onConnect, onDisconnect }: ConnectButtonProps) {
  const isConnected = status === 'connected'
  const isLoading = status === 'connecting'

  return (
    <button
      onClick={isConnected ? onDisconnect : onConnect}
      disabled={isLoading}
      className={`w-full max-w-[280px] sm:max-w-[220px] py-2.5 sm:py-3 border rounded-full text-sm font-semibold cursor-pointer transition-all active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed ${
        isConnected
          ? 'bg-red-500/20 text-red-400 border-red-500/40 hover:bg-red-500/30 hover:border-red-400/60 shadow-[0_0_16px_rgba(239,68,68,0.2)]'
          : 'bg-[#00e5ff]/15 text-[#00e5ff] border-[#00e5ff]/30 hover:bg-[#00e5ff]/25 hover:border-[#00e5ff]/50 shadow-[0_0_20px_rgba(0,229,255,0.15)] hover:shadow-[0_0_28px_rgba(0,229,255,0.25)]'
      }`}
    >
      {isLoading ? 'Connecting...' : isConnected ? 'Disconnect' : 'Connect'}
    </button>
  )
}
