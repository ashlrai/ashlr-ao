import {AbsoluteFill, useCurrentFrame, interpolate, spring, useVideoConfig} from 'remotion';
import {theme} from '../lib/theme';
import {GlassPanel} from '../components/GlassPanel';
import {Logo} from '../components/Logo';

const stats = [
  {label: 'Lines of Code', value: '143K'},
  {label: 'Tests', value: '2,700+'},
  {label: 'Desktop App', value: 'Tauri'},
];

export const ForSale: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  // Grid fade in
  const gridOpacity = interpolate(frame, [0, 20], [0, 0.3], {extrapolateRight: 'clamp'});

  // "FOR SALE" banner entrance
  const bannerScale = spring({frame: frame - 5, fps, config: {damping: 10, mass: 0.6, stiffness: 70}});
  const bannerOpacity = interpolate(frame, [5, 20], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  // Price tag entrance
  const priceScale = spring({frame: frame - 25, fps, config: {damping: 8, mass: 0.4, stiffness: 90}});
  const priceOpacity = interpolate(frame, [25, 40], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  // Price glow pulse
  const pricePulse = interpolate(frame, [40, 80, 120, 160], [0.6, 1, 0.6, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  // Stats panel entrance
  const statsOpacity = interpolate(frame, [50, 65], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const statsY = interpolate(frame, [50, 65], [30, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  // Contact entrance
  const contactOpacity = interpolate(frame, [80, 95], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const contactScale = spring({frame: frame - 80, fps, config: {damping: 10, mass: 0.4}});

  // Logo entrance
  const logoOpacity = interpolate(frame, [95, 110], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const logoScale = spring({frame: frame - 95, fps, config: {damping: 12, mass: 0.5}});

  return (
    <AbsoluteFill style={{backgroundColor: theme.bg, justifyContent: 'center', alignItems: 'center'}}>
      {/* Grid */}
      <div style={{
        position: 'absolute', inset: 0,
        backgroundImage: `linear-gradient(${theme.gridColor} 1px, transparent 1px), linear-gradient(90deg, ${theme.gridColor} 1px, transparent 1px)`,
        backgroundSize: '60px 60px',
        opacity: gridOpacity,
        maskImage: 'radial-gradient(ellipse at 50% 50%, black 0%, transparent 70%)',
        WebkitMaskImage: 'radial-gradient(ellipse at 50% 50%, black 0%, transparent 70%)',
      }} />

      {/* Large ambient glow */}
      <div style={{
        position: 'absolute',
        width: 800, height: 800,
        background: `radial-gradient(circle, ${theme.accentGlow}, transparent 70%)`,
        borderRadius: '50%',
        opacity: bannerOpacity * 0.5,
      }} />

      {/* Price glow */}
      <div style={{
        position: 'absolute',
        width: 500, height: 500,
        background: `radial-gradient(circle, rgba(34, 197, 94, 0.15), transparent 70%)`,
        borderRadius: '50%',
        opacity: priceOpacity * pricePulse,
        top: '25%',
      }} />

      {/* Logo - top */}
      <div style={{
        position: 'absolute',
        top: 80,
        opacity: logoOpacity,
        transform: `scale(${logoScale * 0.8})`,
      }}>
        <Logo />
      </div>

      {/* FOR SALE badge */}
      <div style={{
        position: 'absolute',
        top: 200,
        opacity: bannerOpacity,
        transform: `scale(${bannerScale})`,
      }}>
        <div style={{
          fontFamily: theme.fontDisplay,
          fontSize: 28,
          fontWeight: 700,
          letterSpacing: '0.3em',
          textTransform: 'uppercase',
          color: theme.success,
          padding: '12px 48px',
          border: `2px solid ${theme.success}40`,
          borderRadius: 12,
          background: `${theme.success}10`,
          boxShadow: `0 0 40px ${theme.success}20`,
        }}>
          FOR SALE
        </div>
      </div>

      {/* Price tag */}
      <div style={{
        opacity: priceOpacity,
        transform: `scale(${priceScale})`,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        marginTop: -20,
      }}>
        <div style={{
          fontFamily: theme.fontDisplay,
          fontSize: 120,
          fontWeight: 800,
          letterSpacing: '-0.04em',
          background: `linear-gradient(135deg, #fff, ${theme.accentLight})`,
          WebkitBackgroundClip: 'text',
          WebkitTextFillColor: 'transparent',
          backgroundClip: 'text',
          filter: `drop-shadow(0 0 40px ${theme.accentGlow})`,
          lineHeight: 1,
        }}>
          $9,500
        </div>
        <div style={{
          fontFamily: theme.fontDisplay,
          fontSize: 22,
          color: theme.textSecondary,
          marginTop: 8,
        }}>
          Complete SaaS-Ready Platform
        </div>
      </div>

      {/* Stats panel */}
      <div style={{
        position: 'absolute',
        bottom: 240,
        opacity: statsOpacity,
        transform: `translateY(${statsY}px)`,
      }}>
        <GlassPanel width={900} height={90} opacity={1} scale={1}>
          <div style={{
            display: 'flex',
            justifyContent: 'center',
            alignItems: 'center',
            gap: 64,
            padding: '24px 48px',
          }}>
            {stats.map((stat, i) => {
              const statDelay = 55 + i * 6;
              const statOpacity = interpolate(frame, [statDelay, statDelay + 10], [0, 1], {
                extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
              });
              return (
                <div key={i} style={{
                  display: 'flex', alignItems: 'center', gap: 16,
                  opacity: statOpacity,
                }}>
                  <div style={{
                    fontFamily: theme.fontMono,
                    fontSize: 32,
                    fontWeight: 700,
                    color: theme.accent,
                  }}>
                    {stat.value}
                  </div>
                  <div style={{
                    fontFamily: theme.fontDisplay,
                    fontSize: 18,
                    color: theme.textSecondary,
                  }}>
                    {stat.label}
                  </div>
                </div>
              );
            })}
          </div>
        </GlassPanel>
      </div>

      {/* Contact */}
      <div style={{
        position: 'absolute',
        bottom: 140,
        opacity: contactOpacity,
        transform: `scale(${contactScale})`,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 12,
      }}>
        <div style={{
          fontFamily: theme.fontMono,
          fontSize: 28,
          fontWeight: 600,
          color: theme.accent,
          textShadow: `0 0 30px ${theme.accent}`,
          letterSpacing: '0.02em',
        }}>
          mason@ashlar.pro
        </div>
        <div style={{
          fontFamily: theme.fontDisplay,
          fontSize: 18,
          color: theme.textTertiary,
        }}>
          ashlrao.com
        </div>
      </div>
    </AbsoluteFill>
  );
};
