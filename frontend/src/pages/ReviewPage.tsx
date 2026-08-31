// 审批中心（审核端）—— 按角色显示待办，查看详情 + 批准/退回/作废
// 业务：按钮按 角色×步骤 渲染，与后端 _authorize 权限一致；越权按钮不出现
import { useEffect, useState } from "react";
import { Button, Card, Empty, Input, List, Modal, Space, Typography, message } from "antd";
import { decide, getRequest, listRequests } from "../api/client";
import type { RequestDetail, RequestSummary } from "../api/types";
import { RequestDetailPanel } from "../components/RequestDetailPanel";
import { StatusTag, StepText } from "../components/StatusTag";
import { useRole } from "../context/RoleContext";

const POLL_MS = 5000;

export function ReviewPage() {
  const { role } = useRole();
  const [items, setItems] = useState<RequestSummary[]>([]);
  const [current, setCurrent] = useState<RequestDetail | null>(null);
  const [open, setOpen] = useState(false);
  const [comment, setComment] = useState("");
  const [acting, setActing] = useState(false);

  // 业务：按角色过滤我能操作的挂起点（review=审核人 / leader_decision=领导）
  const isMine = (r: RequestSummary) =>
    role.canReview ? r.current_step === "review" : role.canLead ? r.current_step === "leader_decision" : false;

  const load = async () => {
    try {
      const list = await listRequests();
      setItems(list.filter(isMine));
    } catch {
      /* 轮询失败静默，下个周期重试 */
    }
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [role]);

  const openDetail = async (id: string) => {
    try {
      setCurrent(await getRequest(id));
      setComment("");
      setOpen(true);
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const act = async (action: string) => {
    if (!current) return;
    setActing(true);
    try {
      await decide(current.request_id, { action, comment, actor: role.id });
      message.success(action === "approve" ? "已批准" : action === "return" ? "已退回" : "已作废");
      setOpen(false);
      load();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setActing(false);
    }
  };

  // 当前角色可执行的动作（按 角色×步骤）
  const canActions = role.canReview
    ? ["approve", "return"]
    : role.canLead
      ? ["approve", "void"]
      : [];

  return (
    <Card
      title={`审批中心（${role.label}）`}
      extra={<span style={{ color: "#888" }}>每 5 秒自动刷新</span>}
    >
      {role.canLead && !role.canReview && (
        <Typography.Paragraph type="secondary">
          提示：按公司制度，小额（≤2000）报销免总经理审批，仅直属上级复核并通知您知悉；大额（&gt;2000）才需您终审。
        </Typography.Paragraph>
      )}
      {!role.canReview && !role.canLead && (
        <Empty description="当前角色（报销人/出纳）没有审批待办，请切换到审核人 / 总经理" />
      )}
      <List
        dataSource={items}
        locale={{ emptyText: <Empty description="暂无待办" /> }}
        renderItem={(r) => (
          <List.Item
            actions={[
              <Button key="view" type="primary" onClick={() => openDetail(r.request_id)}>
                查看并处理
              </Button>,
            ]}
          >
            <List.Item.Meta
              title={
                <Space>
                  <span>{r.request_id}</span>
                  <StatusTag status={r.status} />
                  <span style={{ color: "#888" }}>
                    <StepText step={r.current_step} />
                  </span>
                </Space>
              }
              description={`金额 ¥${(r.amount ?? 0).toFixed(2)} · 报销人 ${r.employee_id} · ${r.created_at}`}
            />
          </List.Item>
        )}
      />

      <Modal
        title={current ? `${current.request_id} 详情` : ""}
        open={open}
        width={760}
        footer={null}
        onCancel={() => setOpen(false)}
      >
        {current && (
          <>
            <RequestDetailPanel detail={current} />
            {canActions.length > 0 && (
              <div style={{ marginTop: 16 }}>
                <Typography.Text type="secondary">审批意见（可选）：</Typography.Text>
                <Input.TextArea
                  rows={2}
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                  placeholder="填写意见；退回/作废时建议写明原因"
                  style={{ marginBottom: 12 }}
                />
                <Space>
                  {canActions.includes("approve") && (
                    <Button type="primary" loading={acting} onClick={() => act("approve")}>
                      批准
                    </Button>
                  )}
                  {canActions.includes("return") && (
                    <Button danger loading={acting} onClick={() => act("return")}>
                      退回
                    </Button>
                  )}
                  {canActions.includes("void") && (
                    <Button danger loading={acting} onClick={() => act("void")}>
                      作废
                    </Button>
                  )}
                </Space>
              </div>
            )}
          </>
        )}
      </Modal>
    </Card>
  );
}
