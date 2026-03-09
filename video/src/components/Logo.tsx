import {theme} from '../lib/theme';

export const Logo: React.FC = () => {
  return (
    <div style={{
      width: 120, height: 120,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      position: 'relative',
    }}>
      {/* AO text mark */}
      <div style={{
        fontFamily: theme.fontDisplay,
        fontSize: 64,
        fontWeight: 800,
        letterSpacing: '-0.05em',
        background: `linear-gradient(135deg, ${theme.accentLight}, ${theme.accent}, ${theme.accentDim})`,
        WebkitBackgroundClip: 'text',
        WebkitTextFillColor: 'transparent',
        backgroundClip: 'text',
        filter: `drop-shadow(0 0 25px ${theme.accentGlow})`,
      }}>
        AO
      </div>
    </div>
  );
};
