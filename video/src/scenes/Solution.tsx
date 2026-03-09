import {AbsoluteFill, useCurrentFrame, interpolate, spring, useVideoConfig} from 'remotion';
import {theme} from '../lib/theme';
import {AgentCard} from '../components/AgentCard';
import {GlassPanel} from '../components/GlassPanel';

const agents = [
  {role: 'FE', name: 'auth-ui', color: theme.frontend, status: 'working' as const, summary: 'Writing Login.tsx...'},
  {role: 'BE', name: 'auth-api', color: theme.backend, status: 'working' as const, summary: 'Implementing JWT refresh'},
  {role: 'QA', name: 'e2e-tests', color: theme.tester, status: 'waiting' as const, summary: 'Approve test plan?'},
  {role: 'SEC', name: 'audit', color: theme.security, status: 'complete' as const, summary: '0 vulnerabilities found'},
];

export const Solution: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  // Dashboard frame collapses in
  const dashScale = spring({frame: frame - 10, fps, config: {damping: 12, mass: 0.6}});
  const dashOpacity = interpolate(frame, [10, 30], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  // Title bar
  const titleOpacity = interpolate(frame, [25, 40], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  return (
    <AbsoluteFill style={{backgroundColor: theme.bg}}>
      {/* Grid */}
      <div style={{
        position: 'absolute', inset: 0,
        backgroundImage: `linear-gradient(${theme.gridColor} 1px, transparent 1px), linear-gradient(90deg, ${theme.gridColor} 1px, transparent 1px)`,
        backgroundSize: '60px 60px', opacity: 0.3,
        maskImage: 'radial-gradient(ellipse at 50% 50%, black 0%, transparent 70%)',
        WebkitMaskImage: 'radial-gradient(ellipse at 50% 50%, black 0%, transparent 70%)',
      }} />

      {/* "One command center" text */}
      <div style={{
        position: 'absolute', top: 50, width: '100%', textAlign: 'center',
        fontFamily: theme.fontDisplay, fontSize: 42, fontWeight: 700,
        color: theme.textPrimary,
        opacity: interpolate(frame, [0, 20], [0, 1], {extrapolateRight: 'clamp'}),
      }}>
        One <span style={{color: theme.accent}}>command center</span>
      </div>

      {/* Dashboard frame */}
      <AbsoluteFill style={{
        justifyContent: 'center', alignItems: 'center',
        paddingTop: 80,
      }}>
        <GlassPanel
          width={1400}
          height={700}
          opacity={dashOpacity}
          scale={dashScale}
        >
          {/* Title bar */}
          <div style={{
            display: 'flex', alignItems: 'center',
            padding: '14px 20px',
            borderBottom: `1px solid ${theme.border}`,
            opacity: titleOpacity,
          }}>
            {/* Traffic lights */}
            <div style={{display: 'flex', gap: 8}}>
              <div style={{width: 12, height: 12, borderRadius: '50%', background: '#ff5f57'}} />
              <div style={{width: 12, height: 12, borderRadius: '50%', background: '#febc2e'}} />
              <div style={{width: 12, height: 12, borderRadius: '50%', background: '#28c840'}} />
            </div>
            <div style={{
              flex: 1, textAlign: 'center',
              fontFamily: theme.fontMono, fontSize: 13,
              color: theme.textTertiary,
            }}>
              Ashlr AO — Mission Control
            </div>
            {/* Stats pills */}
            <div style={{display: 'flex', gap: 8}}>
              {['4 agents', 'CPU 34%', 'RAM 2.1G'].map((label, i) => (
                <div key={i} style={{
                  fontFamily: theme.fontMono, fontSize: 11,
                  color: theme.textTertiary,
                  background: 'rgba(255,255,255,0.04)',
                  padding: '4px 10px', borderRadius: 100,
                }}>
                  {label}
                </div>
              ))}
            </div>
          </div>

          {/* Agent cards grid */}
          <div style={{
            display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)',
            gap: 16, padding: 20,
          }}>
            {agents.map((agent, i) => {
              const cardDelay = 40 + i * 15;
              const cardScale = spring({frame: frame - cardDelay, fps, config: {damping: 12, mass: 0.4}});
              const cardOpacity = interpolate(frame, [cardDelay, cardDelay + 10], [0, 1], {
                extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
              });
              return (
                <div key={i} style={{
                  opacity: cardOpacity,
                  transform: `scale(${cardScale}) translateY(${(1 - cardScale) * 20}px)`,
                }}>
                  <AgentCard {...agent} />
                </div>
              );
            })}
          </div>
        </GlassPanel>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
