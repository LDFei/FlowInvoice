// 状态徽标与步骤文案 —— 把后端状态码映射成中文 + 颜色
import { Tag } from "antd";

const STATUS_MAP: Record<string, { color: string; label: string }> = {
  in_review: { color: "processing", label: "审批中" },
  returned: { color: "error", label: "已退回" },
  approved: { color: "success", label: "已批准" },
  paid: { color: "cyan", label: "已打款" },
  voided: { color: "default", label: "已作废" },
};

const STEP_MAP: Record<string, string> = {
  review: "待审核人复核",
  leader_decision: "待领导决策",
  done: "流程结束",
};

export function StatusTag({ status }: { status: string }) {
  const m = STATUS_MAP[status] ?? { color: "default", label: status };
  return <Tag color={m.color}>{m.label}</Tag>;
}

export function StepText({ step }: { step: string }) {
  return <span>{STEP_MAP[step] ?? (step || "—")}</span>;
}
