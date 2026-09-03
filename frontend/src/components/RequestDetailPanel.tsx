// 单据详情面板 —— 审核端/报销端共用的"报销单全貌"展示
// 业务：把 Agent 审核总结 / 政策依据(RAG) / 合规检查 / 审批链 / 审批记录 / 留痕 结构化呈现
import {
  Alert,
  Card,
  Collapse,
  Descriptions,
  Divider,
  List,
  Steps,
  Tag,
  Timeline,
} from "antd";
import type { RequestDetail } from "../api/types";
import { StatusTag, StepText } from "./StatusTag";

const CHAIN_STEPS = ["提交报销", "审核人复核", "领导决策", "批准/打款"];

export function RequestDetailPanel({ detail }: { detail: RequestDetail }) {
  const inv = detail.invoice_data;
  const chain = detail.approval_chain ?? [];
  const records = detail.approval_records ?? [];
  const basis = detail.policy_basis ?? [];
  const checks = detail.compliance_checks ?? [];
  const advance = detail.advance_application;

  return (
    <div>
      {/* 单据头：单号 + 状态 + 当前步骤 */}
      <div style={{ marginBottom: 12 }}>
        <span style={{ fontWeight: 600, fontSize: 16 }}>{detail.request_id}</span>{" "}
        <StatusTag status={detail.status} />
        <span style={{ color: "#888", marginLeft: 8 }}>
          当前：<StepText step={detail.current_step} />
        </span>
      </div>

      {/* 流程进度条（按状态点亮到哪一步） */}
      <Steps
        size="small"
        current={
          detail.status === "approved" || detail.status === "paid" || detail.status === "voided"
            ? 3
            : detail.current_step === "review"
              ? 1
              : 2
        }
        items={CHAIN_STEPS.map((t) => ({ title: t }))}
        style={{ marginBottom: 16 }}
      />

      {/* 退回原因（如有） */}
      {detail.return_reason && (
        <Alert
          type={detail.status === "returned" ? "warning" : "error"}
          showIcon
          message={detail.return_reason.message}
          description={`建议：${detail.return_reason.suggestion}`}
          style={{ marginBottom: 12 }}
        />
      )}

      {/* 票面信息 */}
      {inv && (
        <Card size="small" title="发票信息" style={{ marginBottom: 12 }}>
          <Descriptions column={2} size="small">
            <Descriptions.Item label="发票号码">{inv.invoice_no}</Descriptions.Item>
            <Descriptions.Item label="发票类型">{inv.invoice_type}</Descriptions.Item>
            <Descriptions.Item label="开票日期">{inv.date}</Descriptions.Item>
            <Descriptions.Item label="票面金额">¥{inv.amount.toFixed(2)}</Descriptions.Item>
            <Descriptions.Item label="项目" span={2}>
              {inv.title}
            </Descriptions.Item>
          </Descriptions>
          {inv.risk_flags?.map((f) => (
            <Tag color="orange" key={f}>
              {f}
            </Tag>
          ))}
        </Card>
      )}

      {/* Agent 审核总结 */}
      <Card size="small" title="Agent 审核总结" style={{ marginBottom: 12 }}>
        <div style={{ whiteSpace: "pre-wrap" }}>{detail.summary}</div>
      </Card>

      {/* 政策依据（RAG 检索到的制度条款） */}
      {basis.length > 0 && (
        <Card size="small" title="政策依据（制度条款检索）" style={{ marginBottom: 12 }}>
          <List
            size="small"
            dataSource={basis}
            renderItem={(hit) => (
              <List.Item>
                <div>
                  <Tag color="blue">{hit.clause_id}</Tag>
                  <div style={{ whiteSpace: "pre-wrap", marginTop: 4 }}>{hit.text}</div>
                </div>
              </List.Item>
            )}
          />
        </Card>
      )}

      {/* 合规检查 */}
      {checks.length > 0 && (
        <Card size="small" title="合规检查" style={{ marginBottom: 12 }}>
          <List
            size="small"
            dataSource={checks}
            renderItem={(c) => (
              <List.Item>
                <Tag color={c.passed ? "success" : "error"}>{c.passed ? "通过" : "不通过"}</Tag>
                <span>{c.item}：{c.detail}</span>
              </List.Item>
            )}
          />
        </Card>
      )}

      {/* 事前申请 + 审批链 + 审批记录 */}
      <Descriptions column={1} size="small" bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label="事前申请">
          {advance ? (
            <>
              {advance.app_id}（{advance.start_date} ~ {advance.end_date}，{advance.purpose}）
              <br />
              <span style={{ color: "#888" }}>
                预估 ¥{(advance.estimated_amount ?? 0).toFixed(2)} · 已报 ¥{(advance.reserved_amount ?? 0).toFixed(2)}
                {advance.reserved_amount != null && (
                  <>
                    {" "}
                    · 剩 ¥{Math.max(advance.estimated_amount - advance.reserved_amount, 0).toFixed(2)}
                  </>
                )}
              </span>
            </>
          ) : (
            "无"
          )}
        </Descriptions.Item>
        <Descriptions.Item label="审批链">
          {chain.map((n) => `${n.role}(${n.name})`).join(" → ") || "—"}
        </Descriptions.Item>
      </Descriptions>

      {records.length > 0 && (
        <Card size="small" title="审批记录" style={{ marginBottom: 12 }}>
          <Timeline
            items={records.map((r) => ({
              color: r.decision === "approve" || r.decision === "pay" ? "green" : "red",
              children: (
                <div>
                  <b>{r.role}</b> ·{" "}
                  {r.decision === "approve"
                    ? "批准"
                    : r.decision === "return"
                      ? "退回"
                      : r.decision === "pay"
                        ? "打款"
                        : "作废"}
                  {r.comment ? ` · ${r.comment}` : ""}
                  <div style={{ color: "#999", fontSize: 12 }}>
                    操作人 {r.actor} · {r.time}
                  </div>
                </div>
              ),
            }))}
          />
        </Card>
      )}

      {/* 留痕（通知 + 邮件） */}
      {(detail.messages.length > 0 || detail.emails.length > 0) && (
        <Collapse
          size="small"
          items={[
            {
              key: "trace",
              label: `通知 / 邮件留痕（${detail.messages.length + detail.emails.length}）`,
              children: (
                <List
                  size="small"
                  dataSource={[
                    ...detail.messages.map((m) => `[通知] ${m.to_role} | ${m.content}`),
                    ...detail.emails.map((e) => `[邮件] → ${e.to} | ${e.subject}`),
                  ]}
                  renderItem={(item) => (
                    <List.Item>
                      <div style={{ whiteSpace: "pre-wrap" }}>{item}</div>
                    </List.Item>
                  )}
                />
              ),
            },
          ]}
        />
      )}
    </div>
  );
}
