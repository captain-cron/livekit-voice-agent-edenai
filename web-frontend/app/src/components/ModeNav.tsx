import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'

interface ModeNavProps {
  activeMode: 'pipeline' | 'realtime'
}

export default function ModeNav({ activeMode }: ModeNavProps) {
  const modes = [
    { key: 'pipeline' as const, label: 'Pipeline', href: '/pipeline' },
    { key: 'realtime' as const, label: 'Realtime', href: '/realtime' },
  ]

  return (
    <nav className="flex justify-center gap-1.5 sm:gap-2 mb-1.5 sm:mb-3">
      {modes.map((m) => (
        <Link
          key={m.key}
          to={m.href}
          className={cn(
            'px-3 sm:px-4 py-1 sm:py-1.5 rounded-full text-xs sm:text-sm no-underline border transition-all',
            m.key === activeMode
              ? 'text-[#00e5ff] bg-[#00e5ff]/15 border-[#00e5ff]/30'
              : 'text-muted bg-surface border-border hover:text-[#00e5ff]/70 hover:border-[#00e5ff]/30',
          )}
        >
          {m.label}
        </Link>
      ))}
    </nav>
  )
}
