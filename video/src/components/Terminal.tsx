import {interpolate} from 'remotion';
import {theme} from '../lib/theme';

interface TerminalProps {
  name: string;
  color: string;
  lines: string[];
  frame: number;
  delay: number;
}

export const Terminal: React.FC<TerminalProps> = ({name, color, lines, frame, delay}) => {
  return (
    <div style={{
      background: 'rgba(10, 10, 18, 0.9)',
      border: `1px solid ${theme.border}`,
      borderRadius: 12,
      overflow: 'hidden',
      boxShadow: `0 8px 32px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.03) inset`,
    }}>
      {/* Title bar */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '10px 14px',
        background: 'rgba(17, 17, 24, 0.9)',
        borderBottom: `1px solid ${theme.border}`,
      }}>
        <div style={{display: 'flex', gap: 6}}>
          <div style={{width: 10, height: 10, borderRadius: '50%', background: '#ff5f57'}} />
          <div style={{width: 10, height: 10, borderRadius: '50%', background: '#febc2e'}} />
          <div style={{width: 10, height: 10, borderRadius: '50%', background: '#28c840'}} />
        </div>
        <div style={{
          flex: 1, textAlign: 'center',
          fontFamily: theme.fontMono, fontSize: 12,
          color,
        }}>
          {name}
        </div>
      </div>

      {/* Lines */}
      <div style={{padding: 14, minHeight: 100}}>
        {lines.map((line, i) => {
          const lineDelay = delay + 15 + i * 12;
          const opacity = interpolate(frame, [lineDelay, lineDelay + 8], [0, 1], {
            extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
          });
          return (
            <div key={i} style={{
              fontFamily: theme.fontMono, fontSize: 13,
              color: i === 0 ? theme.textPrimary : theme.textTertiary,
              opacity, lineHeight: 1.8,
            }}>
              {line}
            </div>
          );
        })}
      </div>
    </div>
  );
};
