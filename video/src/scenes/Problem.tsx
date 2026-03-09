import {AbsoluteFill, useCurrentFrame, interpolate, spring, useVideoConfig} from 'remotion';
import {theme} from '../lib/theme';
import {Terminal} from '../components/Terminal';

const terminals = [
  {name: 'Claude Code', color: theme.claude, lines: ['> Implementing auth flow...', '> Writing Login.tsx', '> Running tests...']},
  {name: 'Codex', color: theme.codex, lines: ['> Refactoring API layer...', '> Updating routes.py', '> Checking types...']},
  {name: 'Aider', color: theme.aider, lines: ['> Adding search feature...', '> Creating index.ts', '> Linting files...']},
  {name: 'Goose', color: theme.goose, lines: ['> Security audit in progress', '> Scanning dependencies', '> Reviewing secrets...']},
];

export const Problem: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  // Title fade in
  const titleOpacity = interpolate(frame, [0, 20], [0, 1], {extrapolateRight: 'clamp'});

  // Terminals appear one by one
  const getTerminalStyle = (index: number) => {
    const delay = 10 + index * 12;
    const scale = spring({frame: frame - delay, fps, config: {damping: 12, mass: 0.5}});
    const opacity = interpolate(frame, [delay, delay + 10], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

    // After all appear, they start overwhelming (multiply & scatter)
    const chaosStart = 70;
    const chaos = interpolate(frame, [chaosStart, 120], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

    const offsetX = Math.sin(index * 2.1 + frame * 0.05) * chaos * 80;
    const offsetY = Math.cos(index * 1.7 + frame * 0.04) * chaos * 40;
    const rot = Math.sin(index * 3 + frame * 0.03) * chaos * 8;
    const extraScale = 1 - chaos * 0.15;

    return {
      opacity,
      transform: `scale(${scale * extraScale}) translate(${offsetX}px, ${offsetY}px) rotate(${rot}deg)`,
    };
  };

  // "Too many tabs" text
  const overloadOpacity = interpolate(frame, [80, 100], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const overloadScale = spring({frame: frame - 80, fps, config: {damping: 8, mass: 0.3}});

  return (
    <AbsoluteFill style={{backgroundColor: theme.bg}}>
      {/* Grid */}
      <div style={{
        position: 'absolute', inset: 0,
        backgroundImage: `linear-gradient(${theme.gridColor} 1px, transparent 1px), linear-gradient(90deg, ${theme.gridColor} 1px, transparent 1px)`,
        backgroundSize: '60px 60px',
        opacity: 0.3,
        maskImage: 'radial-gradient(ellipse at 50% 50%, black 0%, transparent 70%)',
        WebkitMaskImage: 'radial-gradient(ellipse at 50% 50%, black 0%, transparent 70%)',
      }} />

      {/* Title */}
      <div style={{
        position: 'absolute', top: 80, width: '100%', textAlign: 'center',
        fontFamily: theme.fontDisplay, fontSize: 36, fontWeight: 600,
        color: theme.textSecondary, opacity: titleOpacity,
      }}>
        Managing AI agents across terminals?
      </div>

      {/* Terminal grid */}
      <div style={{
        position: 'absolute', top: 180, left: 0, right: 0,
        display: 'flex', justifyContent: 'center', gap: 24, flexWrap: 'wrap',
        padding: '0 120px',
      }}>
        {terminals.map((t, i) => (
          <div key={i} style={{...getTerminalStyle(i), width: 400}}>
            <Terminal name={t.name} color={t.color} lines={t.lines} frame={frame} delay={10 + i * 12} />
          </div>
        ))}
      </div>

      {/* Overwhelm text */}
      <div style={{
        position: 'absolute', bottom: 100, width: '100%', textAlign: 'center',
        fontFamily: theme.fontDisplay, fontSize: 48, fontWeight: 700,
        color: theme.error, opacity: overloadOpacity,
        transform: `scale(${overloadScale})`,
        textShadow: `0 0 40px ${theme.error}`,
      }}>
        Chaos.
      </div>
    </AbsoluteFill>
  );
};
