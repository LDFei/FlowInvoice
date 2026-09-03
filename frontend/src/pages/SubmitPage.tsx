// 提交报销页(个人报销通道)—— 上传票据 + 填单(报销方式 + 可选关联事前申请)→ 提交 → 展示 Agent 审核结果
// 业务:员工个人垫付费用统一走本通道,页面不选"业务方向"——票种由系统识别,归入当前已开通的费用类型组审核。
//       报销方式两种(默认关联事前申请):
//         1) 关联出差申请(默认,差旅场景):出差前已提交出差/事前申请,报销挂到本次出差 → 占用其预算池;
//            不指定申请时,后端仅【唯一命中】的区间才自动挂靠,多份重叠需显式指定(#97)。
//         2) 直接报销:未做事前申请,凭票直接报销,不进预算池——日常大部分报销属于此路,不应被"必须有申请"卡住。
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
  Spin,
  Steps,
  Tag,
  Upload,
  message,
} from "antd";
import { useEffect, useState } from "react";
import { getRequest, getSubmission, listAdvances, submitReimburse } from "../api/client";
import type { AdvanceApplication, RequestDetail } from "../api/types";
import { RequestDetailPanel } from "../components/RequestDetailPanel";
import { StatusTag } from "../components/StatusTag";
import { useRole } from "../context/RoleContext";

/** ¥ 格式化（负数=超支） */
function fmt(n: number | undefined): string {
  return `¥${(n ?? 0).toFixed(2)}`;
}

// 异步提交的分步进度展示（#53：提交即 202 受理，后台 worker 处理；动画按耗时推进，终态以轮询结果为准）
const ASYNC_STAGES = ["提交受理", "识别票据", "核实票真伪", "合规检查", "生成审批链", "等待人工复核"];

export function SubmitPage() {
  const { role } = useRole();
  const [form] = Form.useForm();
  const [file, setFile] = useState<File | null>(null);
  const [advances, setAdvances] = useState<AdvanceApplication[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<RequestDetail | null>(null);
  const [error, setError] = useState("");
  // 异步提交：202 受理后的任务号 + 已耗秒数（进度动画 + 轮询；终态 = 轮询到 succeeded）
  const [asyncId, setAsyncId] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  // 报销方式：advance=关联出差申请（默认，差旅报销挂到已批事前申请）/ direct=直接报销（未做事前申请）
  const mode = Form.useWatch("mode", form) ?? "advance";

  // 作用：拉当前报销人的事前申请，供"关联出差申请"下拉选择（Active 才有匹配价值）
  useEffect(() => {
    let alive = true;
    listAdvances("active", role.id)
      .then((items) => {
        if (alive) setAdvances(items);
      })
      .catch(() => {
        /* 静默：拿不到列表时仅自动匹配 */
      });
    return () => {
      alive = false;
    };
  }, [role.id]);

  // 作用：异步提交轮询（#53）——202 受理后每秒推进进度动画、每 2s 查一次任务状态；
  //       succeeded → 拉单据详情展示；failed → 给失败原因可重提；超长(240s)提示去列表查
  useEffect(() => {
    if (!asyncId) return;
    let alive = true;
    let sec = 0;
    const timer = setInterval(async () => {
      sec += 1;
      setElapsed(sec);
      if (sec % 2 !== 0) return; // 每秒只推进度，整秒轮询任务
      try {
        const sub = await getSubmission(asyncId);
        if (!alive) return;
        if (sub.status === "succeeded") {
          const detail = await getRequest(asyncId);
          if (!alive) return;
          setResult(detail);
          setError("");
          setAsyncId(null);
          message.success("Agent 已完成审核，单据进入审批");
        } else if (sub.status === "failed") {
          setError(`后台处理失败：${sub.error?.message ?? "未知错误"}，可修正后重新提交`);
          setAsyncId(null);
        } else if (sec > 240) {
          setError("处理耗时较长，仍在后台执行——可稍后到「我的单据」查看结果");
          setAsyncId(null);
        }
      } catch {
        /* 轮询瞬时失败（连接抖动/任务未落库）忽略，下轮重试 */
      }
    }, 1000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [asyncId]);

  const handleSubmit = async (values: {
    purpose?: string;
    declared_amount?: number;
    mode?: string;
    app_id?: string;
  }) => {
    if (!file) {
      message.warning("请先上传发票文件");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const chosenMode = values.mode ?? "advance";
      const fd = new FormData();
      fd.append("file", file);
      // 业务：方向是系统内部路由键（费用类型组），不由用户选择——后端默认 travel（差旅费用组）。
      //       mode 决定本次报销是否关联事前申请：advance（默认）→ 可显式带 app_id，空则由后端唯一命中自动挂靠；
      //       direct（直接报销）→ 不带 app_id，后端跳过事前匹配与预算占用。
      fd.append("mode", chosenMode);
      fd.append("purpose", values.purpose ?? "");
      fd.append("declared_amount", String(values.declared_amount ?? 0));
      fd.append("employee_id", role.id);
      if (chosenMode === "advance" && values.app_id) fd.append("app_id", values.app_id);
      const res = await submitReimburse(fd);
      if (!("accepted" in res)) {
        // 同步模式（FLOWINVOICE_ASYNC 未开）：直接拿到完整详情
        setResult(res);
        message.success("已提交，Agent 开始自动审核");
        return;
      }
      // 异步模式：202 受理 → 进入进度动画面板，后台 worker 处理，轮询到 succeeded 再展示结果
      setElapsed(0);
      setAsyncId(res.request_id);
      message.success("已受理，Agent 正在后台处理");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card title="提交报销" style={{ maxWidth: 720 }}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={`当前报销人：${role.name}（${role.id}）`}
          description="个人报销通道：员工因公垫付后凭票据申请，公司审核打款。本期已开通【差旅费用组】（火车票/机票/酒店/出差打车）。报销方式默认【关联出差申请】——出差前已提交出差/事前申请，报销挂到本次出差、占用其预算；若本次费用没有做事前申请，切换上方「直接报销」即可凭票直接提交，不进预算池。Demo 用 demo/样例-火车票.txt 上传即可走通：提交后 Agent 自动完成 识别票种 → 验真 → 合规检查 → 审批链 → 审核总结。"
        />
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          initialValues={{ declared_amount: 528.5, mode: "advance" }}
        >
          <Form.Item label="发票文件" required>
            <Upload.Dragger
              accept=".txt,.png,.jpg,.jpeg,.bmp,.webp,.pdf,.xml,.ofd"
              maxCount={1}
              beforeUpload={() => false}
              onChange={({ fileList }) => setFile(fileList[0]?.originFileObj ?? null)}
            >
              <p className="ant-upload-drag-icon"><InboxOutlined /></p>
              <p className="ant-upload-text">点击或拖拽发票文件到此处</p>
              <p className="ant-upload-hint">支持文本票面、数电票 XML/OFD、PDF 与图片/扫描件；票种由系统自动识别</p>
            </Upload.Dragger>
          </Form.Item>
          <Form.Item label="报销事由" name="purpose">
            <Input placeholder="如：上海客户拜访往返高铁" />
          </Form.Item>
          <Form.Item
            label="申报金额（元）"
            name="declared_amount"
            extra="Agent 会与票面金额比对，差异超阈值将标记风险"
          >
            <InputNumber min={0} precision={2} style={{ width: "100%" }} />
          </Form.Item>
          {/* 报销方式：默认关联出差申请（差旅报销挂到已批事前申请、占预算）；直接报销=未做事前申请，凭票直报 */}
          <Form.Item label="报销方式" name="mode" style={{ marginBottom: mode === "advance" ? 8 : 16 }}>
            <Radio.Group>
              <Radio value="advance">关联出差申请（默认）</Radio>
              <Radio value="direct">直接报销（未做事前申请）</Radio>
            </Radio.Group>
          </Form.Item>
          {mode === "advance" ? (
            <Form.Item
              label="关联出差申请（可选）"
              name="app_id"
              extra="本次差旅报销默认挂到已批的出差/事前申请：可从下拉选择本次出差申请；不指定时，系统仅在【唯一一份】有效申请的区间覆盖开票日期时自动挂靠。若本次费用没有做事前申请，请切换上方「直接报销」。"
            >
              <Select
                allowClear
                placeholder="自动匹配（不指定）"
                options={advances
                  .filter((a) => a.direction === "travel") // 本期唯一费用类型组=差旅；多组并存后按组过滤
                  .map((a) => {
                    const remaining = a.remaining_amount ?? a.estimated_amount - (a.reserved_amount ?? 0);
                    return {
                      value: a.app_id,
                      label: `${a.app_id} · ${a.start_date} ~ ${a.end_date} · 已报 ${fmt(a.reserved_amount)} · 剩 ${fmt(remaining)}`,
                    };
                  })}
                optionRender={(opt) => {
                  const a = advances.find((x) => x.app_id === opt.value);
                  const remaining = a ? (a.remaining_amount ?? a.estimated_amount - (a.reserved_amount ?? 0)) : 0;
                  return (
                    <Space>
                      <span>{a?.app_id}</span>
                      <span style={{ color: "#888" }}>
                        {a?.start_date} ~ {a?.end_date}
                      </span>
                      <Tag color={remaining < 0 ? "red" : "green"}>{remaining < 0 ? `超支 ${fmt(-remaining)}` : `剩 ${fmt(remaining)}`}</Tag>
                    </Space>
                  );
                }}
                notFoundContent="无有效出差申请：可在「事前申请」页创建，或切换上方「直接报销」提交"
              />
            </Form.Item>
          ) : (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message="直接报销"
              description="本次报销不关联事前申请、不占用出差预算；凭票直接进入 验真 → 合规检查 → 审批链。"
            />
          )}
          <Button type="primary" htmlType="submit" loading={submitting} disabled={!!asyncId} block>
            {asyncId ? "后台处理中…" : "提交报销，交给 Agent 审核"}
          </Button>
        </Form>
        {error && <Alert type="error" showIcon message={error} style={{ marginTop: 12 }} />}
      </Card>

      {/* 异步提交进度（#53）：202 受理后展示分步动画，轮询 succeeded 后切换到结果卡片 */}
      {asyncId && (
        <Card title="正在提交报销（异步后台处理）" style={{ maxWidth: 720 }}>
          <Steps
            current={Math.min(ASYNC_STAGES.length - 1, Math.floor(elapsed / 6))}
            items={ASYNC_STAGES.map((title) => ({ title }))}
            size="small"
          />
          <div style={{ marginTop: 12, color: "#666" }}>
            <Spin size="small" style={{ marginRight: 8 }} />
            已受理单号 <b>{asyncId}</b>，Agent 正在后台执行（已耗时 {elapsed} 秒）。识别 → 核实真伪 → 合规
            按序推进，完成后自动展示审核结果，请勿重复提交。
          </div>
        </Card>
      )}

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
