import type { Tone } from "@/lib/format";

/** Tailwind classes for each semantic tone (colours are tokens in globals.css). */
export const TONE_TEXT: Record<Tone, string> = {
  discount: "text-[var(--sem-discount)]",
  fair: "text-[var(--sem-fair)]",
  premium: "text-[var(--sem-premium)]",
  unknown: "text-[var(--sem-unknown)]",
};
export const TONE_CHIP: Record<Tone, string> = {
  discount: "bg-[var(--sem-discount-bg)] text-[var(--sem-discount)]",
  fair: "bg-[var(--sem-fair-bg)] text-[var(--sem-fair)]",
  premium: "bg-[var(--sem-premium-bg)] text-[var(--sem-premium)]",
  unknown: "bg-[var(--sem-unknown-bg)] text-[var(--sem-unknown)]",
};
export const TONE_VAR: Record<Tone, string> = {
  discount: "var(--sem-discount)",
  fair: "var(--sem-fair)",
  premium: "var(--sem-premium)",
  unknown: "var(--sem-unknown)",
};
