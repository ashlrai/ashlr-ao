import {AbsoluteFill, useCurrentFrame, interpolate, spring, useVideoConfig, Sequence} from 'remotion';
import {theme} from '../lib/theme';
import {GlassPanel} from '../components/GlassPanel';

const features = [
  {
    title: 'Multi-Spawn',
    subtitle: 'Launch entire fleets at once',
    icon: '4x',
    color: theme.accent,
    detail: 'Fleet presets deploy 2-3 agents simultaneously. Full Stack, Review Team, Quality Check — one click.',
  },
  {
    title: 'Real-Time Monitoring',
    subtitle: 'Live terminal capture every second',
    icon: '1s',
    color: theme.success,
    detail: 'ANSI-colored output, status detection, activity feed, file conflict warnings — all streaming.',
  },
  {
    title: 'Auto-Pilot',
    subtitle: 'Hands-free orchestration',
    icon: 'AP',
    color: theme.warning,
    detail: 'Auto-restart stalled agents. Auto-approve safe patterns. Health-based pause. Never-approve safety list.',
  },
  {
    title: 'Intelligence Layer',
    subtitle: 'LLM-powered fleet analysis',
    icon: 'AI',
    color: '#EC4899',
    detail: 'Natural language commands. Agent summaries. Cross-agent conflict detection every 30 seconds.',
  },
];

const FeatureSlide: React.FC<{feature: typeof features[0]; index: number}> = ({feature}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  const scale = spring({frame: frame - 5, fps, config: {damping: 12, mass: 0.5}});
  const opacity = interpolate(frame, [0, 15], [0, 1], {extrapolateRight: 'clamp'});

  const detailOpacity = interpolate(frame, [20, 35], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const detailY = interpolate(frame, [20, 35], [15, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', backgroundColor: theme.bg}}>
      {/* Grid */}
      <div style={{
        position: 'absolute', inset: 0,
        backgroundImage: `linear-gradient(${theme.gridColor} 1px, transparent 1px), linear-gradient(90deg, ${theme.gridColor} 1px, transparent 1px)`,
        backgroundSize: '60px 60px', opacity: 0.3,
        maskImage: 'radial-gradient(ellipse at 50% 50%, black 0%, transparent 70%)',
        WebkitMaskImage: 'radial-gradient(ellipse at 50% 50%, black 0%, transparent 70%)',
      }} />

      <div style={{
        opacity, transform: `scale(${scale})`,
        display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 32,
      }}>
        {/* Icon badge */}
        <div style={{
          width: 120, height: 120, borderRadius: 24,
          background: `${feature.color}15`,
          border: `2px solid ${feature.color}40`,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontFamily: theme.fontMono, fontSize: 36, fontWeight: 700,
          color: feature.color,
          boxShadow: `0 0 60px ${feature.color}30`,
        }}>
          {feature.icon}
        </div>

        {/* Title */}
        <div style={{
          fontFamily: theme.fontDisplay, fontSize: 56, fontWeight: 700,
          color: theme.textPrimary, letterSpacing: '-0.03em',
        }}>
          {feature.title}
        </div>

        {/* Subtitle */}
        <div style={{
          fontFamily: theme.fontDisplay, fontSize: 28,
          color: feature.color, fontWeight: 500,
        }}>
          {feature.subtitle}
        </div>

        {/* Detail */}
        <GlassPanel width={800} height={80} opacity={detailOpacity} scale={1}>
          <div style={{
            padding: '20px 32px',
            fontFamily: theme.fontDisplay, fontSize: 20,
            color: theme.textSecondary, textAlign: 'center',
            transform: `translateY(${detailY}px)`,
          }}>
            {feature.detail}
          </div>
        </GlassPanel>
      </div>
    </AbsoluteFill>
  );
};

export const Features: React.FC = () => {
  return (
    <AbsoluteFill>
      {features.map((feature, i) => (
        <Sequence key={i} from={i * 60} durationInFrames={60}>
          <FeatureSlide feature={feature} index={i} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
