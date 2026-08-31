// App 布局 —— 顶部角色切换 + 菜单 + 内容区
import { Layout, Menu, Select, Typography } from "antd";
import { useState } from "react";
import { ROLES, RoleProvider, useRole } from "./context/RoleContext";
import { AdvancePage } from "./pages/AdvancePage";
import { FinancePage } from "./pages/FinancePage";
import { MyRequestsPage } from "./pages/MyRequestsPage";
import { ReviewPage } from "./pages/ReviewPage";
import { SubmitPage } from "./pages/SubmitPage";

const MENU_ITEMS = [
  { key: "submit", label: "提交报销" },
  { key: "review", label: "审批中心" },
  { key: "finance", label: "出纳端" },
  { key: "mine", label: "我的报销" },
  { key: "advance", label: "事前申请" },
];

function Shell() {
  const { role, setRole } = useRole();
  const [page, setPage] = useState("submit");

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Header style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <Typography.Title level={4} style={{ color: "#fff", margin: 0, whiteSpace: "nowrap" }}>
          FlowInvoice · 报销 Agent
        </Typography.Title>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[page]}
          items={MENU_ITEMS}
          onClick={({ key }) => setPage(key)}
          style={{ flex: 1, minWidth: 0, borderBottom: "none" }}
        />
        <Select
          value={role.id}
          style={{ width: 220 }}
          onChange={(id) => setRole(ROLES.find((r) => r.id === id)!)}
          options={ROLES.map((r) => ({ value: r.id, label: `${r.label} · ${r.name}（${r.id}）` }))}
        />
      </Layout.Header>
      <Layout.Content style={{ padding: 24, maxWidth: 1080, width: "100%", margin: "0 auto" }}>
        {page === "submit" && <SubmitPage />}
        {page === "review" && <ReviewPage />}
        {page === "finance" && <FinancePage />}
        {page === "mine" && <MyRequestsPage />}
        {page === "advance" && <AdvancePage />}
      </Layout.Content>
    </Layout>
  );
}

export default function App() {
  return (
    <RoleProvider>
      <Shell />
    </RoleProvider>
  );
}
