// 出纳端（财务）—— 待打款清单 + 确认打款
// 业务：审批≠支付——财务不参与审批，仅在 approved 后执行打款；确认后单据 approved → paid
import { useEffect, useState } from "react";
import {
  Button,
  Card,
  Empty,
  Input,
  List,
  Modal,
  Space,
  Tabs,
  Typography,
  message,
} from "antd";
import { getRequest, listRequests, pay } from "../api/client";
import type { RequestDetail, RequestSummary } from "../api/types";
import { RequestDetailPanel } from "../components/RequestDetailPanel";
import { StatusTag } from "../components/StatusTag";
import { useRole } from "../context/RoleContext";

const POLL_MS = 5000;

export function FinancePage() {
  const { role } = useRole();
  const [pending, setPending] = useState<RequestSummary[]>([]);
  const [paid, setPaid] = useState<RequestSummary[]>([]);
  const [current, setCurrent] = useState<RequestDetail | null>(null);
  const [open, setOpen] = useState(false);
  const [comment, setComment] = useState("");
  const [acting, setActing] = useState(false);

  const load = async () => {
    try {
      const [a, b] = await Promise.all([listRequests("approved"), listRequests("paid")]);
      setPending(a);
      setPaid(b);
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

  const confirmPay = async () => {
    if (!current) return;
    setActing(true);
    try {
      await pay(current.request_id, { comment, actor: role.id });
      message.success("已确认打款，报销人已收到到账通知");
      setOpen(false);
      load();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setActing(false);
    }
  };

  const renderList = (items: RequestSummary[], emptyText: string) => (
    <List
      dataSource={items}
      locale={{ emptyText: <Empty description={emptyText} /> }}
      renderItem={(r) => (
        <List.Item
          actions={[
            <Button key="view" type="primary" onClick={() => openDetail(r.request_id)}>
              查看
            </Button>,
          ]}
        >
          <List.Item.Meta
            title={
              <Space>
                <span>{r.request_id}</span>
                <StatusTag status={r.status} />
              </Space>
            }
            description={`金额 ¥${(r.amount ?? 0).toFixed(2)} · 报销人 ${r.employee_id} · ${r.created_at}`}
          />
        </List.Item>
      )}
    />
  );

  return (
    <Card
      title={`出纳端（${role.name}）`}
      extra={<span style={{ color: "#888" }}>每 5 秒自动刷新</span>}
    >
      {role.id !== "3001" && (
        <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
          当前角色不是出纳（3001），以下为只读示例视图；切换顶部角色为「出纳 · 赵六（3001）」可执行打款。
        </Typography.Paragraph>
      )}
      <Tabs
        items={[
          {
            key: "pending",
            label: `待打款（${pending.length}）`,
            children: renderList(pending, "暂无待打款单据（审批通过后进入此处）"),
          },
          {
            key: "paid",
            label: `已打款（${paid.length}）`,
            children: renderList(paid, "暂无已打款单据"),
          },
        ]}
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
            {current.status === "approved" && role.id === "3001" && (
              <div style={{ marginTop: 16 }}>
                <Typography.Text type="secondary">打款备注（可选，如转账流水号）：</Typography.Text>
                <Input.TextArea
                  rows={2}
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                  placeholder="填写转账流水号等备注"
                  style={{ marginBottom: 12 }}
                />
                <Button type="primary" danger loading={acting} onClick={confirmPay}>
                  确认打款
                </Button>
              </div>
            )}
          </>
        )}
      </Modal>
    </Card>
  );
}
