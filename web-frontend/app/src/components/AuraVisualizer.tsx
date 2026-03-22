import { AgentAudioVisualizerAura } from '@/components/agents-ui/agent-audio-visualizer-aura'
import type { RemoteAudioTrack } from 'livekit-client'
import type { AgentState } from '@/hooks/agents-ui/use-agent-audio-visualizer-aura'

export type AuraMode = 'disconnected' | 'idle' | 'user-speaking' | 'agent-speaking'

const MODE_TO_STATE: Record<AuraMode, AgentState> = {
  disconnected: 'disconnected',
  idle: 'listening',
  'user-speaking': 'listening',
  'agent-speaking': 'speaking',
}

interface AuraVisualizerProps {
  auraMode: AuraMode
  agentTrack: RemoteAudioTrack | null
}

export default function AuraVisualizer({ auraMode, agentTrack }: AuraVisualizerProps) {
  return (
    <div className="w-full h-full flex items-center justify-center">
      <AgentAudioVisualizerAura
        state={MODE_TO_STATE[auraMode]}
        audioTrack={agentTrack ?? undefined}
        color="#00e5ff"
        colorShift={0.3}
        themeMode="dark"
        className="!h-auto w-full max-w-[min(80vw,420px)] sm:max-w-[480px] aspect-square"
      />
    </div>
  )
}
