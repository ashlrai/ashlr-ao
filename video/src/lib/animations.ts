import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

export const useSpring = (
  delay: number = 0,
  config?: {damping?: number; mass?: number; stiffness?: number}
) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  return spring({
    frame: frame - delay,
    fps,
    config: {
      damping: config?.damping ?? 12,
      mass: config?.mass ?? 0.5,
      stiffness: config?.stiffness ?? 80,
    },
  });
};

export const useFadeIn = (delay: number = 0, duration: number = 15) => {
  const frame = useCurrentFrame();
  return interpolate(frame, [delay, delay + duration], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
};

export const useSlideUp = (delay: number = 0, distance: number = 40) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  const progress = spring({
    frame: frame - delay,
    fps,
    config: {damping: 12, mass: 0.5, stiffness: 80},
  });

  return {
    opacity: progress,
    transform: `translateY(${interpolate(progress, [0, 1], [distance, 0])}px)`,
  };
};

export const useTypewriter = (text: string, delay: number = 0, speed: number = 2) => {
  const frame = useCurrentFrame();
  const elapsed = Math.max(0, frame - delay);
  const chars = Math.min(Math.floor(elapsed / speed), text.length);
  return text.slice(0, chars);
};

export const useScale = (delay: number = 0) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  return spring({
    frame: frame - delay,
    fps,
    config: {damping: 10, mass: 0.4, stiffness: 100},
  });
};
