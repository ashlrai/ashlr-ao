import {AbsoluteFill, useCurrentFrame, interpolate, spring, useVideoConfig} from 'remotion';
import {theme} from '../lib/theme';
import {GlassPanel} from '../components/GlassPanel';

export const Install: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  const panelScale = spring({frame: frame - 5, fps, config: {damping: 12, mass: 0.5}});
  const panelOpacity = interpolate(frame, [5, 20], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  // Line 1: pip install
  const line1 = '$ pip install ashlr-ao';
  const elapsed1 = Math.max(0, frame - 20);
  const chars1 = Math.min(Math.floor(elapsed1 / 1.5), line1.length);

  // Line 2: ashlr
  const line2Start = 55;
  const line2 = '$ ashlr';
  const elapsed2 = Math.max(0, frame - line2Start);
  const chars2 = Math.min(Math.floor(elapsed2 / 2), line2.length);

  // Line 3: output
  const line3Start = 72;
  const line3Opacity = interpolate(frame, [line3Start, line3Start + 10], [0, 1], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });

  // URL highlight
  const urlStart = 85;
  const urlOpacity = interpolate(frame, [urlStart, urlStart + 10], [0, 1], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });
  const urlScale = spring({frame: frame - urlStart, fps, config: {damping: 10, mass: 0.4}});

  return (
    <AbsoluteFill style={{backgroundColor: theme.bg, justifyContent: 'center', alignItems: 'center'}}>
      {/* Grid */}
      <div style={{
        position: 'absolute', inset: 0,
        backgroundImage: `linear-gradient(${theme.gridColor} 1px, transparent 1px), linear-gradient(90deg, ${theme.gridColor} 1px, transparent 1px)`,
        backgroundSize: '60px 60px', opacity: 0.3,
        maskImage: 'radial-gradient(ellipse at 50% 50%, black 0%, transparent 70%)',
        WebkitMaskImage: 'radial-gradient(ellipse at 50% 50%, black 0%, transparent 70%)',
      }} />

      {/* "Get started in 30 seconds" */}
      <div style={{
        position: 'absolute', top: 120,
        fontFamily: theme.fontDisplay, fontSize: 42, fontWeight: 700,
        color: theme.textPrimary,
        opacity: interpolate(frame, [0, 15], [0, 1], {extrapolateRight: 'clamp'}),
      }}>
        Get started in <span style={{color: theme.accent}}>30 seconds</span>
      </div>

      {/* Terminal panel */}
      <GlassPanel width={900} height={360} opacity={panelOpacity} scale={panelScale}>
        <div style={{padding: 32}}>
          {/* Terminal header */}
          <div style={{
            display: 'flex', gap: 8, marginBottom: 24,
          }}>
            <div style={{width: 12, height: 12, borderRadius: '50%', background: '#ff5f57'}} />
            <div style={{width: 12, height: 12, borderRadius: '50%', background: '#febc2e'}} />
            <div style={{width: 12, height: 12, borderRadius: '50%', background: '#28c840'}} />
          </div>

          {/* Lines */}
          <div style={{fontFamily: theme.fontMono, fontSize: 22, lineHeight: 2}}>
            <div style={{color: theme.textPrimary}}>
              {line1.slice(0, chars1)}
              {chars1 < line1.length && <span style={{
                opacity: frame % 20 < 10 ? 1 : 0, color: theme.accent,
              }}>_</span>}
            </div>

            {chars1 >= line1.length && (
              <div style={{color: theme.success, opacity: interpolate(frame, [45, 50], [0, 1], {
                extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
              })}}>
                Successfully installed ashlr-ao-1.6.1
              </div>
            )}

            {frame >= line2Start && (
              <div style={{color: theme.textPrimary, marginTop: 8}}>
                {line2.slice(0, chars2)}
                {chars2 < line2.length && chars2 > 0 && <span style={{
                  opacity: frame % 20 < 10 ? 1 : 0, color: theme.accent,
                }}>_</span>}
              </div>
            )}

            {frame >= line3Start && (
              <div style={{color: theme.textTertiary, opacity: line3Opacity, marginTop: 4}}>
                Dashboard running at{' '}
                <span style={{
                  color: theme.accent, opacity: urlOpacity,
                  textShadow: `0 0 20px ${theme.accent}`,
                  transform: `scale(${urlScale})`,
                  display: 'inline-block',
                }}>
                  http://127.0.0.1:5111
                </span>
              </div>
            )}
          </div>
        </div>
      </GlassPanel>
    </AbsoluteFill>
  );
};
