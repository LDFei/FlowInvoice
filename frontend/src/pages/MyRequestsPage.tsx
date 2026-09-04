// 我的报销（报销端）—— 我提交的单据列表 + 状态跟踪 + 退回原因 + 处理失败的任务（可重试）
import { useEffect, useState } from "react";
import { Button, Card, Empty, List, Modal, Space, Tag, message } from "antd";
import { getRequest, listRequests, listSubmissions, retrySubmission } from "../api/client";
import type { RequestDetail, RequestSummary, SubmissionStatus } from "../api/types";
import { RequestDetailPanel } from "../components/RequestDetailPanel";
import { StatusTag, StepText } from "../components/StatusTag";
import { useRole } from "../context/RoleContext";

const POLL_MS = 5000;

export function MyRequestsPage() {
  const { role } = useRole();
  const [items, setItems] = useState<RequestSummary[]>([]);
  // 处理失败的任务（#53 闭环）：异步模式下失败不产生报销单，只有这里可见——提供"重试"重新投递
  const [failures, setFailures] = useState<SubmissionStatus[]>([]);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [detail, setDetail] = useState<RequestDetail | null>(null);
  const [open, setOpen] = useState(false);

  const load = async () => {
    try {
      const [list, failed] = await Promise.all([
        listRequests(undefined, { employee_id: role.id }), // #70 服务端按申报人过滤，不拉全量
        listSubmissions({ employeeId: role.id, status: "failed" }),
      ]);
      setItems(list);
      setFailures(failed);
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

  // 失败任务重试：failed → pending（后端重新投递）；刷新后任务离开失败区，成功即出现在"我的报销"
  const retryFailed = async (requestId: string) => {
    setRetrying(requestId);
    try {
      await retrySubmission(requestId);
      message.success("已重新投递，Agent 正在后台处理");
      load();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setRetrying(null);
    }
  };

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
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

      {/* 处理失败的任务：失败不产生报销单，仅在异步模式出现；重试后任务转 pending，成功会进入上方列表 */}
      {failures.length > 0 && (
        <Card
          title="处理失败的任务"
          extra={<span style={{ color: "#888" }}>失败单不生成报销单，可修正输入后重试</span>}
        >
          <List
            dataSource={failures}
            locale={{ emptyText: <Empty description="暂无失败任务" /> }}
            renderItem={(s) => (
              <List.Item
                actions={[
                  <Button
                    key="retry"
                    size="small"
                    type="primary"
                    danger
                    loading={retrying === s.request_id}
                    onClick={() => retryFailed(s.request_id)}
                  >
                    重试
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <Space>
                      <span>{s.request_id}</span>
                      <Tag color="red">处理失败</Tag>
                      <span style={{ color: "#888" }}>已尝试 {s.attempts} 次</span>
                    </Space>
                  }
                  description={`失败原因：${s.error?.message ?? s.error?.type ?? "未知错误"} · ${s.updated_at}`}
                />
              </List.Item>
            )}
          />
        </Card>
      )}
    </Space>
  );
}
