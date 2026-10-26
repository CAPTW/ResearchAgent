import { type ButtonHTMLAttributes, type ReactNode } from "react";
import { SpinnerGap } from "@phosphor-icons/react";
import { Line, LineChart, ResponsiveContainer } from "recharts";

export function V2Panel({
  title,
  subtitle,
  action,
  className = "",
  children,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={`v2-panel ${className}`.trim()}>
      <header className="v2-panel-header">
        <div><h2>{title}</h2>{subtitle ? <p>{subtitle}</p> : null}</div>
        {action ? <div className="v2-panel-action">{action}</div> : null}
      </header>
      <div className="v2-panel-body">{children}</div>
    </section>
  );
}

export function V2Kpi({
  label,
  value,
  delta,
  values,
  tone = "blue",
  icon,
}: {
  label: string;
  value: string | number;
  delta?: string;
  values?: number[];
  tone?: "blue" | "purple" | "green" | "amber" | "cyan";
  icon?: ReactNode;
}) {
  const data = (values ?? []).map((point, index) => ({ index, point }));
  return (
    <article className={`v2-kpi tone-${tone}${data.length ? "" : " without-chart"}`}>
      <div className="v2-kpi-copy">
        <span>{icon}<em>{label}</em></span>
        <strong>{value}</strong>
        {delta ? <small>{delta}</small> : null}
      </div>
      {data.length ? <div className="v2-kpi-chart" aria-hidden="true">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data}>
            <Line type="monotone" dataKey="point" stroke="currentColor" strokeWidth={2} dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div> : null}
    </article>
  );
}

export function V2Toggle({
  checked,
  label,
  disabled,
  onChange,
}: {
  checked: boolean;
  label: string;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={`v2-toggle${checked ? " is-on" : ""}`}
      onClick={() => onChange(!checked)}
    >
      <span />
    </button>
  );
}

export function V2Button({ busy, children, className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { busy?: boolean }) {
  return (
    <button {...props} className={`v2-button ${className}`.trim()} disabled={busy || props.disabled}>
      {busy ? <SpinnerGap className="v2-spin" size={15} /> : null}{children}
    </button>
  );
}

export function V2Loading({ label }: { label: string }) {
  return <div className="v2-state-card" aria-busy="true"><SpinnerGap className="v2-spin" size={22} /><strong>{label}</strong></div>;
}

export function V2Error({ title = "화면을 불러오지 못했습니다.", message, onRetry, onDismiss }: { title?: string; message: string; onRetry?: () => void; onDismiss?: () => void }) {
  return <div className="v2-state-card is-error" role="alert"><strong>{title}</strong><p>{message}</p><span>{onRetry ? <V2Button onClick={onRetry}>다시 시도</V2Button> : null}{onDismiss ? <V2Button onClick={onDismiss}>닫기</V2Button> : null}</span></div>;
}

export function V2Unavailable({ title, reason, onRetry }: { title: string; reason: string; onRetry?: () => void }) {
  return <div className="v2-state-card is-unavailable" role="status"><strong>{title}</strong><p>{reason}</p>{onRetry ? <V2Button onClick={onRetry}>해당 리소스 다시 시도</V2Button> : null}</div>;
}

export function V2Empty({ title, description }: { title: string; description: string }) {
  return <div className="v2-empty"><strong>{title}</strong><p>{description}</p></div>;
}

export const V2_CHART_COLORS = [
  "var(--chart-series-1)",
  "var(--chart-series-2)",
  "var(--chart-series-3)",
  "var(--chart-series-4)",
  "var(--chart-series-5)",
  "var(--chart-series-6)",
];

export function readV2ChartColor(index: number): string {
  if (typeof document === "undefined") return ["#3f82ff", "#8d62d9", "#2f9f62", "#b57d00", "#168997", "#d05268"][index % 6];
  const value = getComputedStyle(document.documentElement).getPropertyValue(`--chart-series-${(index % 6) + 1}`).trim();
  return value || ["#3f82ff", "#8d62d9", "#2f9f62", "#b57d00", "#168997", "#d05268"][index % 6];
}

export const V2_TOOLTIP_STYLE = {
  background: "var(--surface-elevated)",
  border: "1px solid var(--border-strong)",
  borderRadius: 6,
  color: "var(--text-primary)",
  fontSize: 11,
};
