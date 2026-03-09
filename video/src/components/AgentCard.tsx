import {theme} from '../lib/theme';

interface AgentCardProps {
  role: string;
  name: string;
  color: string;
  status: 'working' | 'waiting' | 'complete';
  summary: string;
}

const statusStyles = {
  working: {color: theme.accentLight, bg: 'rgba(112, 108, 240, 0.1)', dotColor: theme.accent},
  waiting: {color: '#F59E0B', bg: 'rgba(245, 158, 11, 0.1)', dotColor: '#F59E0B'},
  complete: {color: theme.success, bg: 'rgba(34, 197, 94, 0.1)', dotColor: theme.success},
};

const statusLabels = {working: 'working', waiting: 'waiting', complete: 'done'};

export const AgentCard: React.FC<AgentCardProps> = ({role, name, color, status, summary}) => {
  const s = statusStyles[status];

  return (
    <div style={{
      background: 'rgba(14, 14, 24, 0.6)',
      border: `1px solid ${theme.border}`,
      borderRadius: 12,
      padding: 18,
    }}>
      {/* Top row */}
      <div style={{display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12}}>
        {/* Role badge */}
        <div style={{
          width: 36, height: 36, borderRadius: 8,
          background: `${color}20`,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontFamily: theme.fontMono, fontSize: 13, fontWeight: 700,
          color,
        }}>
          {role}
        </div>

        {/* Name */}
        <div style={{
          flex: 1,
          fontFamily: theme.fontDisplay, fontSize: 16, fontWeight: 600,
          color: theme.textPrimary,
        }}>
          {name}
        </div>

        {/* Status */}
        <div style={{
          display: 'flex', alignItems: 'center', gap: 5,
          fontFamily: theme.fontMono, fontSize: 11, fontWeight: 600,
          textTransform: 'uppercase' as const, letterSpacing: '0.05em',
          padding: '3px 10px', borderRadius: 100,
          color: s.color, background: s.bg,
        }}>
          <div style={{
            width: 6, height: 6, borderRadius: '50%',
            background: s.dotColor,
            boxShadow: `0 0 8px ${s.dotColor}`,
          }} />
          {statusLabels[status]}
        </div>
      </div>

      {/* Summary */}
      <div style={{
        fontFamily: theme.fontMono, fontSize: 13,
        color: status === 'complete' ? theme.success : status === 'waiting' ? '#F59E0B' : theme.textTertiary,
        lineHeight: 1.6,
      }}>
        {summary}
      </div>

      {/* Progress bar */}
      {status === 'working' && (
        <div style={{
          height: 2, marginTop: 12,
          background: 'rgba(255,255,255,0.04)',
          borderRadius: 1, overflow: 'hidden',
        }}>
          <div style={{
            height: '100%', width: '65%',
            background: `linear-gradient(90deg, ${color}, ${color}66)`,
            borderRadius: 1,
          }} />
        </div>
      )}
    </div>
  );
};
