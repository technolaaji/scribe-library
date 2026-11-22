/**
 * Botanical ornaments: a library as a garden of knowledge. A sprig for
 * section labels, a flower between leaves for rules (a fleuron, literally),
 * and a small garden growing out of the ground for the masthead.
 * All drawn in currentColor so they follow the text colour.
 */

const STEM = { fill: "none", stroke: "currentColor", strokeLinecap: "round" as const };

/** Five petals around a centre, with the centre left open. */
function Bloom({
  cx,
  cy,
  r = 4,
  petal = 2.4,
  hole = "var(--bg)",
}: {
  cx: number;
  cy: number;
  r?: number;
  petal?: number;
  /** colour of the open centre: whatever the bloom is drawn on */
  hole?: string;
}) {
  return (
    <g>
      {[0, 72, 144, 216, 288].map((a) => (
        <ellipse
          key={a}
          cx={cx}
          cy={cy - r * 0.55}
          rx={petal * 0.62}
          ry={r * 0.55}
          transform={`rotate(${a} ${cx} ${cy})`}
          fill="currentColor"
        />
      ))}
      <circle cx={cx} cy={cy} r={r * 0.3} fill={hole} />
      <circle cx={cx} cy={cy} r={r * 0.13} fill="currentColor" />
    </g>
  );
}

/** A leaf from (x, y), pointing along `angle` degrees (0 = right). */
function Leaf({ x, y, angle, size = 1 }: { x: number; y: number; angle: number; size?: number }) {
  return (
    <path
      d="M0 0C3 -2.6 7 -2.8 10 0C7 2.8 3 2.6 0 0Z"
      transform={`translate(${x} ${y}) rotate(${angle}) scale(${size})`}
      fill="currentColor"
    />
  );
}

/** A slender upright sprig: a stem, alternating leaves, a bud. */
export function Sprig({
  height = 18,
  className,
  style,
}: {
  height?: number | string;
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <svg
      viewBox="0 0 24 48"
      height={height}
      width={typeof height === "number" ? height / 2 : undefined}
      className={className}
      style={{ display: "inline-block", ...style }}
      aria-hidden
    >
      <path d="M12 47C12.5 36 11 24 12 8" {...STEM} strokeWidth={1.3} />
      <Leaf x={12.2} y={38} angle={-35} size={0.95} />
      <Leaf x={11.8} y={30} angle={-145} size={0.95} />
      <Leaf x={11.6} y={22} angle={-38} size={0.8} />
      <Leaf x={11.8} y={15} angle={-140} size={0.7} />
      <ellipse cx={12} cy={5.6} rx={2.4} ry={3.6} fill="currentColor" />
    </svg>
  );
}

/** ——— ❀ ——— : a horizontal rule with a flower between two leafy branches. */
export function Fleuron({
  short = true,
  className = "",
  height = 26,
}: {
  short?: boolean;
  className?: string;
  height?: number;
}) {
  const branch = (
    <g>
      <path d="M4 12.5C11 12.8 18 12 25.5 12" {...STEM} strokeWidth={1.1} />
      <Leaf x={20} y={12.1} angle={-150} size={0.8} />
      <Leaf x={14} y={12.5} angle={155} size={0.7} />
      <Leaf x={9} y={12.6} angle={-155} size={0.55} />
    </g>
  );
  return (
    <div className={`fleuron ${short ? "fleuron-short" : ""} ${className}`} aria-hidden>
      <svg viewBox="0 0 64 24" height={height} width={(height * 64) / 24}>
        {branch}
        <g transform="matrix(-1 0 0 1 64 0)">{branch}</g>
        <Bloom cx={32} cy={12} r={7} petal={4.6} />
      </svg>
    </div>
  );
}

/** A small garden: a flowering stem between two budding branches, on a mound. */
export function Garden({ width = 56, className }: { width?: number; className?: string }) {
  const side = (
    <g>
      <path d="M30.5 40.5C27 34 21 29.5 14.5 26.5" {...STEM} strokeWidth={1.3} />
      <Leaf x={23} y={31.2} angle={-120} size={0.9} />
      <Leaf x={26.5} y={35} angle={160} size={0.8} />
      <ellipse cx={13} cy={25.6} rx={2} ry={3.2} transform="rotate(-62 13 25.6)" fill="currentColor" />
    </g>
  );
  return (
    <svg
      viewBox="0 0 64 44"
      width={width}
      height={(width * 44) / 64}
      className={className}
      aria-hidden
    >
      {side}
      <g transform="matrix(-1 0 0 1 64 0)">{side}</g>
      <path d="M32 41C32.4 32 31.3 23 32 14.5" {...STEM} strokeWidth={1.5} />
      <Leaf x={32.2} y={30} angle={-30} size={1.05} />
      <Leaf x={31.8} y={23.5} angle={-150} size={0.95} />
      <Bloom cx={32} cy={9} r={8} petal={5.4} />
      <path d="M6 42.2C18 39.6 46 39.6 58 42.2" {...STEM} strokeWidth={1.4} />
    </svg>
  );
}

/**
 * A flowering branch for the page margin: it grows from the left and its
 * bloom points right, at the line where a handwritten note was written.
 * Stretches to whatever width the margin allows.
 */
export function MarginBranch({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 60 24" preserveAspectRatio="xMaxYMid meet" className={className} aria-hidden>
      <path d="M2 15C14 15.5 26 13 46 12" {...STEM} strokeWidth={1.4} />
      <Leaf x={14} y={15} angle={-140} size={0.85} />
      <Leaf x={22} y={14.4} angle={150} size={0.75} />
      <Leaf x={31} y={13.4} angle={-150} size={0.7} />
      <path d="M38 12.6L42 7.5" {...STEM} strokeWidth={1.1} />
      <ellipse cx={43} cy={6.4} rx={1.5} ry={2.4} transform="rotate(35 43 6.4)" fill="currentColor" />
      <Bloom cx={51} cy={12} r={8} petal={5.2} hole="#f4f3ef" />
    </svg>
  );
}
