import {AbsoluteFill, useCurrentFrame, interpolate, spring, useVideoConfig} from 'remotion';
import {theme} from '../lib/theme';
import {Logo} from '../components/Logo';

export const Outro: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  const logoScale = spring({frame: frame - 5, fps, config: {damping: 10, mass: 0.5}});
  const logoOpacity = interpolate(frame, [5, 20], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  const taglineOpacity = interpolate(frame, [25, 45], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const taglineY = interpolate(frame, [25, 45], [20, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  const urlOpacity = interpolate(frame, [50, 65], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const urlScale = spring({frame: frame - 50, fps, config: {damping: 10, mass: 0.4}});

  // Pillars
  const pillars = ['Open Source', 'Local-First', 'Zero Config'];

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

      {/* Glow */}
      <div style={{
        position: 'absolute',
        width: 600, height: 600,
        background: `radial-gradient(circle, ${theme.accentGlow}, transparent 70%)`,
        borderRadius: '50%',
        opacity: logoOpacity * 0.6,
      }} />

      {/* Logo */}
      <div style={{
        transform: `scale(${logoScale * 1.5})`,
        opacity: logoOpacity,
        marginBottom: 48,
      }}>
        <Logo />
      </div>

      {/* Pillars */}
      <div style={{
        display: 'flex', gap: 48, marginTop: 20,
        opacity: taglineOpacity,
        transform: `translateY(${taglineY}px)`,
      }}>
        {pillars.map((pillar, i) => {
          const delay = 30 + i * 8;
          const pillOpacity = interpolate(frame, [delay, delay + 12], [0, 1], {
            extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
          });
          return (
            <div key={i} style={{
              fontFamily: theme.fontDisplay, fontSize: 28, fontWeight: 600,
              color: theme.textPrimary, opacity: pillOpacity,
              display: 'flex', alignItems: 'center', gap: 12,
            }}>
              <div style={{
                width: 8, height: 8, borderRadius: '50%',
                background: theme.accent,
                boxShadow: `0 0 12px ${theme.accent}`,
              }} />
              {pillar}
            </div>
          );
        })}
      </div>

      {/* URL */}
      <div style={{
        marginTop: 64,
        fontFamily: theme.fontMono, fontSize: 32, fontWeight: 600,
        color: theme.accent, opacity: urlOpacity,
        transform: `scale(${urlScale})`,
        textShadow: `0 0 30px ${theme.accent}`,
        letterSpacing: '0.02em',
      }}>
        ashlrao.com
      </div>
    </AbsoluteFill>
  );
};
