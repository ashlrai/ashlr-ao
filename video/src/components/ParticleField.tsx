import {useCurrentFrame} from 'remotion';
import {theme} from '../lib/theme';

interface ParticleFieldProps {
  opacity: number;
}

export const ParticleField: React.FC<ParticleFieldProps> = ({opacity}) => {
  const frame = useCurrentFrame();

  // Generate deterministic particles
  const particles = Array.from({length: 40}, (_, i) => {
    const seed = i * 137.508;
    const x = ((seed * 7.3) % 1920);
    const y = ((seed * 13.7) % 1080);
    const size = 1.5 + (seed % 3);
    const colors = [theme.accent, theme.accentLight, theme.accentDim, theme.backend, theme.frontend];
    const color = colors[i % colors.length];
    const speed = 0.3 + (seed % 1) * 0.4;
    const phase = (seed * 2.3) % (Math.PI * 2);

    return {x, y, size, color, speed, phase};
  });

  return (
    <div style={{position: 'absolute', inset: 0, opacity}}>
      {particles.map((p, i) => {
        const px = p.x + Math.sin(frame * 0.01 * p.speed + p.phase) * 30;
        const py = p.y + Math.cos(frame * 0.008 * p.speed + p.phase) * 20;
        const pulseAlpha = 0.3 + Math.sin(frame * 0.04 + p.phase) * 0.2;

        return (
          <div key={i} style={{
            position: 'absolute',
            left: px, top: py,
            width: p.size * 2, height: p.size * 2,
            borderRadius: '50%',
            background: p.color,
            opacity: pulseAlpha,
            boxShadow: `0 0 ${p.size * 4}px ${p.color}`,
          }} />
        );
      })}
    </div>
  );
};
