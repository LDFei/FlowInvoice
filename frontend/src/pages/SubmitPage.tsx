// 提交报销页（报销端）—— 上传发票 + 填表 → 提交 → 展示 Agent 审核结果
import { InboxOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Radio,
  Select,
  Space,
  Upload,
  message,
} from "antd";
import { useState } from "react";
import { submitReimburse } from "../api/client";
import type { RequestDetail } from "../api/types";
import { RequestDetailPanel } from "../components/RequestDetailPanel";
import { StatusTag } from "../components/StatusTag";
import { useRole } from "../context/RoleContext";

export function SubmitPage() {
  const { role } = useRole();
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<RequestDetail | null>(null);
  const [error, setError] = useState("");

  const handleSubmit = async (values: {
    direction: string;
    purpose: string;
    declared_amount: number;
    payment_method: string;
  }) => {
    if (!file) {
      message.warning("请先上传发票文件");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("direction", values.direction);
      form.append("purpose", values.purpose);
      form.append("declared_amount", String(values.declared_amount));
      form.append("payment_method", values.payment_method);
      form.append("employee_id", role.id);
      const detail = await submitReimburse(form);
      setResult(detail);
      message.success("已提交，Agent 开始自动审核");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card title="提交差旅报销" style={{ maxWidth: 720 }}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={`当前报销人：${role.name}（${role.id}）`}
          description="Demo 发票为文本票面，可用项目 demo/样例-火车票.txt 上传。提交后 Agent 自动完成 识别 → 验真 → 匹配事前申请 → 合规检查 → 生成审批链。"
        />
        <Form layout="vertical" onFinish={handleSubmit} initialValues={{ direction: "travel", payment_method: "personal", declared_amount: 528.5 }}>
          <Form.Item label="发票文件" required>
            <Upload.Dragger
              accept=".txt,.png,.jpg,.jpeg"
              maxCount={1}
              beforeUpload={() => false}
              onChange={({ fileList }) => setFile(fileList[0]?.originFileObj ?? null)}
            >
              <p className="ant-upload-drag-icon"><InboxOutlined /></p>
              <p className="ant-upload-text">点击或拖拽发票文件到此处</p>
              <p className="ant-upload-hint">Demo 支持文本票面（如 demo/样例-火车票.txt）</p>
            </Upload.Dragger>
          </Form.Item>
          <Form.Item label="业务方向" name="direction" rules={[{ required: true }]}>
            <Select
              options={[
                { value: "travel", label: "差旅" },
                { value: "office", label: "办公用品（二期）" },
              ]}
            />
          </Form.Item>
          <Form.Item label="报销事由" name="purpose" rules={[{ required: true, message: "请填写报销事由" }]}>
            <Input placeholder="如：上海客户拜访差旅" />
          </Form.Item>
          <Form.Item label="申报金额（元）" name="declared_amount" rules={[{ required: true }]}>
            <InputNumber min={0} precision={2} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="支付方式" name="payment_method">
            <Radio.Group
              options={[
                { value: "personal", label: "个人垫付" },
                { value: "corporate", label: "对公付款" },
              ]}
            />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={submitting} block>
            提交报销，交给 Agent 审核
          </Button>
        </Form>
        {error && <Alert type="error" showIcon message={error} style={{ marginTop: 12 }} />}
      </Card>

      {result && (
        <Card
          title={
            <span>
              提交结果 <StatusTag status={result.status} />
            </span>
          }
          style={{ maxWidth: 720 }}
        >
          <RequestDetailPanel detail={result} />
        </Card>
      )}
    </Space>
  );
}
