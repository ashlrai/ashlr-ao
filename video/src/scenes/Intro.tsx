import {AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, spring} from 'remotion';
import {theme} from '../lib/theme';
import {ParticleField} from '../components/ParticleField';
import {Logo} from '../components/Logo';

export const Intro: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  // Grid fade in
  const gridOpacity = interpolate(frame, [0, 30], [0, 0.4], {extrapolateRight: 'clamp'});

  // Logo scale up with ring
  const logoScale = spring({frame: frame - 15, fps, config: {damping: 10, mass: 0.5, stiffness: 60}});
  const logoOpacity = interpolate(frame, [15, 30], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  // Ring animation
  const ringScale = spring({frame: frame - 25, fps, config: {damping: 8, mass: 0.3, stiffness: 50}});
  const ringRotation = interpolate(frame, [25, 120], [0, 360]);

  // Tagline typewriter
  const tagline = 'Your AI agents, one command center';
  const tagDelay = 50;
  const elapsed = Math.max(0, frame - tagDelay);
  const chars = Math.min(Math.floor(elapsed / 1.5), tagline.length);
  const displayText = tagline.slice(0, chars);

  // Subtitle fade
  const subOpacity = interpolate(frame, [90, 110], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const subY = interpolate(frame, [90, 110], [20, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  return (
    <AbsoluteFill style={{backgroundColor: theme.bg}}>
      <ParticleField opacity={gridOpacity} />

      {/* Grid lines */}
      <div style={{
        position: 'absolute', inset: 0,
        backgroundImage: `linear-gradient(${theme.gridColor} 1px, transparent 1px), linear-gradient(90deg, ${theme.gridColor} 1px, transparent 1px)`,
        backgroundSize: '60px 60px',
        opacity: gridOpacity,
        maskImage: 'radial-gradient(ellipse at 50% 50%, black 0%, transparent 70%)',
        WebkitMaskImage: 'radial-gradient(ellipse at 50% 50%, black 0%, transparent 70%)',
      }} />

      {/* Center content */}
      <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center'}}>
        {/* Logo with rings */}
        <div style={{
          position: 'relative',
          width: 200, height: 200,
          transform: `scale(${logoScale})`,
          opacity: logoOpacity,
        }}>
          {/* Outer ring */}
          <div style={{
            position: 'absolute', inset: -20,
            borderRadius: '50%',
            border: `2px solid ${theme.borderAccent}`,
            transform: `scale(${ringScale}) rotate(${ringRotation}deg)`,
            opacity: ringScale * 0.6,
          }}>
            <div style={{
              position: 'absolute', top: -5, left: '50%',
              width: 10, height: 10, borderRadius: '50%',
              background: theme.accent,
              boxShadow: `0 0 20px ${theme.accent}`,
            }} />
          </div>

          {/* Inner ring */}
          <div style={{
            position: 'absolute', inset: -40,
            borderRadius: '50%',
            border: `1px solid rgba(112, 108, 240, 0.12)`,
            transform: `scale(${ringScale}) rotate(${-ringRotation * 0.7}deg)`,
            opacity: ringScale * 0.4,
          }}>
            <div style={{
              position: 'absolute', top: -4, left: '50%',
              width: 8, height: 8, borderRadius: '50%',
              background: theme.accentDim,
              boxShadow: `0 0 15px ${theme.accentDim}`,
            }} />
          </div>

          {/* Glow */}
          <div style={{
            position: 'absolute', inset: -60,
            background: `radial-gradient(circle, ${theme.accentGlow}, transparent 70%)`,
            borderRadius: '50%',
            opacity: logoOpacity * 0.8,
          }} />

          <Logo />
        </div>

        {/* Tagline */}
        <div style={{
          marginTop: 60,
          fontFamily: theme.fontDisplay,
          fontSize: 56,
          fontWeight: 700,
          color: theme.textPrimary,
          letterSpacing: '-0.03em',
          textAlign: 'center',
          lineHeight: 1.1,
        }}>
          {displayText}
          <span style={{
            opacity: frame % 30 < 15 ? 1 : 0,
            color: theme.accent,
          }}>|</span>
        </div>

        {/* Subtitle */}
        <div style={{
          marginTop: 24,
          fontFamily: theme.fontDisplay,
          fontSize: 24,
          color: theme.textSecondary,
          opacity: subOpacity,
          transform: `translateY(${subY}px)`,
        }}>
          Orchestrate Claude Code, Codex, Aider & Goose
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
