import { Fleuron } from "app/components/ornaments";

/* A colophon, as printers set at the end of a book. */
export default function Footer() {
  return (
    <footer className="mt-20 mb-12 text-center">
      <Fleuron short={false} className="mb-6" />
      <p className="text-[0.95rem] italic faint">
        Kept offline, on this machine. Set in Garamond &amp; Cinzel.
      </p>
      <p className="mt-2 text-[0.85rem] faint">
        <span className="kbd">←</span> <span className="kbd">→</span> turn pages ·{" "}
        <span className="kbd">f</span> full screen · <span className="kbd">esc</span> close
      </p>
    </footer>
  );
}
