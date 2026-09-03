// 事前申请（报销端）—— 差旅报销前置条件：创建申请 + 列表
import { DatePicker, Form, Input, InputNumber, List, Space, Tag, Button, Card, message } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { useEffect, useState } from "react";
import { createAdvance, listAdvances } from "../api/client";
import type { AdvanceApplication } from "../api/types";
import { useRole } from "../context/RoleContext";

const ADVANCE_STATUS: Record<string, { color: string; label: string }> = {
  active: { color: "green", label: "有效" },
  expired: { color: "default", label: "已过期" },
  // used 仅兼容旧库遗留数据（预算池模型下不再自动置，#91）
  used: { color: "blue", label: "已核销(旧)" },
};

export function AdvancePage() {
  const { role } = useRole();
  const [form] = Form.useForm();
  const [items, setItems] = useState<AdvanceApplication[]>([]);
  const [creating, setCreating] = useState(false);

  const load = async () => {
    try {
      setItems(await listAdvances(undefined, role.id)); // 附 已占用/剩余
    } catch {
      /* 静默 */
    }
  };
  useEffect(() => {
    void load(); // 首次加载
  }, []);

  const handleCreate = async (values: {
    start_date: Dayjs;
    end_date: Dayjs;
    estimated_amount: number;
    purpose: string;
  }) => {
    setCreating(true);
    try {
      await createAdvance({
        employee_id: role.id,
        direction: "travel",
        start_date: values.start_date.format("YYYY-MM-DD"),
        end_date: values.end_date.format("YYYY-MM-DD"),
        estimated_amount: values.estimated_amount,
        purpose: values.purpose,
      });
      message.success("事前申请已创建");
      form.resetFields();
      load();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setCreating(false);
    }
  };

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card title="创建事前申请单" style={{ maxWidth: 560 }}>
        <Form form={form} layout="vertical" onFinish={handleCreate} initialValues={{ estimated_amount: 2000 }}>
          <Form.Item label="出差开始日期" name="start_date" rules={[{ required: true }]}>
            <DatePicker style={{ width: "100%" }} disabledDate={(d) => d.isBefore(dayjs(), "day")} />
          </Form.Item>
          <Form.Item label="出差结束日期" name="end_date" rules={[{ required: true }]}>
            <DatePicker style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="预估金额（元）" name="estimated_amount" rules={[{ required: true }]}>
            <InputNumber min={0} precision={2} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="出差事由" name="purpose" rules={[{ required: true, message: "请填写事由" }]}>
            <Input placeholder="如：上海客户拜访" />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={creating} block>
            创建申请
          </Button>
        </Form>
      </Card>

      <Card title="我的事前申请">
        <List
          dataSource={items.filter((a) => a.employee_id === role.id)}
          locale={{ emptyText: "暂无事前申请" }}
          renderItem={(a) => {
            const st = ADVANCE_STATUS[a.status] ?? { color: "default", label: a.status };
            const reserved = a.reserved_amount ?? 0;
            const remaining = a.remaining_amount ?? a.estimated_amount - reserved;
            const over = remaining < 0;
            return (
              <List.Item>
                <List.Item.Meta
                  title={
                    <Space>
                      <span>{a.app_id}</span>
                      <Tag color={st.color}>{st.label}</Tag>
                      <Tag color={over ? "red" : "green"}>
                        {over ? `超支 ${Math.abs(remaining).toFixed(2)}` : `剩余 ¥${remaining.toFixed(2)}`}
                      </Tag>
                    </Space>
                  }
                  description={
                    <>
                      <span>
                        {a.start_date} ~ {a.end_date} · 有效期至 {a.valid_until} · 预估 ¥{a.estimated_amount.toFixed(2)} ·{" "}
                        {a.purpose}
                      </span>
                      <br />
                      <span style={{ color: "#888" }}>
                        已报（approved 占用合计）¥{reserved.toFixed(2)} —— 报销提交页可选本申请挂靠报销
                      </span>
                    </>
                  }
                />
              </List.Item>
            );
          }}
        />
      </Card>
    </Space>
  );
}
