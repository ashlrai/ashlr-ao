import {useCurrentFrame} from 'remotion';
import {theme} from '../lib/theme';

interface TypeWriterProps {
  text: string;
  delay?: number;
  speed?: number;
  fontSize?: number;
  color?: string;
  showCursor?: boolean;
}

export const TypeWriter: React.FC<TypeWriterProps> = ({
  text,
  delay = 0,
  speed = 2,
  fontSize = 24,
  color = theme.textPrimary,
  showCursor = true,
}) => {
  const frame = useCurrentFrame();
  const elapsed = Math.max(0, frame - delay);
  const chars = Math.min(Math.floor(elapsed / speed), text.length);
  const displayText = text.slice(0, chars);
  const cursorVisible = showCursor && chars < text.length && frame % 20 < 10;

  return (
    <span style={{
      fontFamily: theme.fontMono,
      fontSize,
      color,
    }}>
      {displayText}
      {cursorVisible && <span style={{color: theme.accent}}>|</span>}
    </span>
  );
};
