import React from 'react';
import {theme} from '../lib/theme';

interface GlassPanelProps {
  width: number;
  height: number;
  opacity: number;
  scale: number;
  children: React.ReactNode;
}

export const GlassPanel: React.FC<GlassPanelProps> = ({width, height, opacity, scale, children}) => {
  return (
    <div style={{
      width, minHeight: height,
      background: 'rgba(10, 10, 18, 0.85)',
      border: `1px solid ${theme.border}`,
      borderRadius: 20,
      overflow: 'hidden',
      opacity,
      transform: `scale(${scale})`,
      boxShadow: `
        0 0 0 1px rgba(255, 255, 255, 0.03) inset,
        0 24px 80px rgba(0, 0, 0, 0.6),
        0 0 120px ${theme.accentGlow}
      `,
    }}>
      {children}
    </div>
  );
};
