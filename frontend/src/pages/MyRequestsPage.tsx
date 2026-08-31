// 我的报销（报销端）—— 我提交的单据列表 + 状态跟踪 + 退回原因
import { useEffect, useState } from "react";
import { Button, Card, Empty, List, Modal, Space, message } from "antd";
import { getRequest, listRequests } from "../api/client";
import type { RequestDetail, RequestSummary } from "../api/types";
import { RequestDetailPanel } from "../components/RequestDetailPanel";
import { StatusTag, StepText } from "../components/StatusTag";
import { useRole } from "../context/RoleContext";

const POLL_MS = 5000;

export function MyRequestsPage() {
  const { role } = useRole();
  const [items, setItems] = useState<RequestSummary[]>([]);
  const [detail, setDetail] = useState<RequestDetail | null>(null);
  const [open, setOpen] = useState(false);

  const load = async () => {
    try {
      const list = await listRequests();
      setItems(list.filter((r) => r.employee_id === role.id));
    } catch {
      /* 轮询失败静默 */
    }
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [role]);

  const openDetail = async (id: string) => {
    try {
      setDetail(await getRequest(id));
      setOpen(true);
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  return (
    <Card title={`我的报销（${role.name}）`} extra={<span style={{ color: "#888" }}>每 5 秒自动刷新</span>}>
      <List
        dataSource={items}
        locale={{ emptyText: <Empty description="还没有报销单" /> }}
        renderItem={(r) => (
          <List.Item
            actions={[
              <Button key="view" type="link" onClick={() => openDetail(r.request_id)}>
                查看详情
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
              description={`金额 ¥${(r.amount ?? 0).toFixed(2)} · ${r.created_at}`}
            />
          </List.Item>
        )}
      />
      <Modal title={detail ? `${detail.request_id} 详情` : ""} open={open} width={760} footer={null} onCancel={() => setOpen(false)}>
        {detail && <RequestDetailPanel detail={detail} />}
      </Modal>
    </Card>
  );
}
