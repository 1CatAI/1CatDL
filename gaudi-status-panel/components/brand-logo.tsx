type BrandLogoProps = {
  className?: string;
  compact?: boolean;
};

// Preserve the supplied PNG. Viewports frame the wordmark or its cat-shaped M.
export function BrandLogo({ className = '', compact = false }: BrandLogoProps) {
  return (
    <svg
      className={`brand-logo ${className}`.trim()}
      viewBox={compact ? '515 208 528 421' : '63 208 1905 421'}
      width={compact ? 40 : 163}
      height={compact ? 32 : 36}
      aria-label="YMZX"
      focusable="false"
    >
      <title>YMZX</title>
      <image href="/brand/ymzx-29675b01.png" width="2018" height="783" />
    </svg>
  );
}
