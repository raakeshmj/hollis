import { Route, Routes } from "react-router";
import { Shell } from "./components/Shell";
import { AgentEdit } from "./pages/AgentEdit";
import { Agents } from "./pages/Agents";
import { Approvals } from "./pages/Approvals";
import { Collection } from "./pages/Collection";
import { Knowledge } from "./pages/Knowledge";
import { Login } from "./pages/Login";
import { Memory } from "./pages/Memory";
import { Models } from "./pages/Models";
import { NotFound } from "./pages/NotFound";
import { Overview } from "./pages/Overview";
import { RunDetail } from "./pages/RunDetail";
import { Runs } from "./pages/Runs";
import { Settings } from "./pages/Settings";
import { Tools } from "./pages/Tools";

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<Shell />}>
        <Route index element={<Overview />} />
        <Route path="runs" element={<Runs />} />
        <Route path="runs/:id" element={<RunDetail />} />
        <Route path="approvals" element={<Approvals />} />
        <Route path="agents" element={<Agents />} />
        <Route path="agents/new" element={<AgentEdit />} />
        <Route path="agents/:name" element={<AgentEdit />} />
        <Route path="tools" element={<Tools />} />
        <Route path="models" element={<Models />} />
        <Route path="knowledge" element={<Knowledge />} />
        <Route path="knowledge/:name" element={<Collection />} />
        <Route path="memory" element={<Memory />} />
        <Route path="settings" element={<Settings />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
