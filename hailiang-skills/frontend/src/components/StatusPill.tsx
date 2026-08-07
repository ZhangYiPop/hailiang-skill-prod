type StatusPillProps = {
  label: string;
  tone?: "default" | "success" | "warning" | "danger" | "info";
};

const toneClasses = {
  default: "border-white/10 bg-white/5 text-slate-200",
  success: "border-emerald-400/30 bg-emerald-400/10 text-emerald-200",
  warning: "border-amber-400/30 bg-amber-400/10 text-amber-100",
  danger: "border-rose-400/30 bg-rose-400/10 text-rose-200",
  info: "border-cyan-400/30 bg-cyan-400/10 text-cyan-200",
};

export function StatusPill({ label, tone = "default" }: StatusPillProps) {
  return (
    <span
      className={[
        "inline-flex items-center rounded-full border px-3 py-1 text-[11px] font-medium tracking-[0.18em] uppercase",
        toneClasses[tone],
      ].join(" ")}
    >
      {label}
    </span>
  );
}
