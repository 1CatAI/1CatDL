'use client';

import { useEffect, useRef, useState } from 'react';
import { adminRequest } from './admin-model';

export type PlacementPolicy = { enabled: boolean; preferredNode: string };
export type PlacementQuote = {
  policy: PlacementPolicy; fallbackAllowed: boolean; reasonCodes: string[]; reason: string;
  recommendedNode: string | null;
  nodes: Array<{ id: string; allowed: boolean; createAvailable: boolean; gpuAvailable: boolean; reason: string }>;
};

export function PlacementSettings({ onSaved }: { onSaved: () => void }) {
  const [saved, setSaved] = useState<PlacementPolicy | null>(null);
  const [draft, setDraft] = useState<PlacementPolicy>({ enabled: true, preferredNode: 'G2-002' });
  const [nodes, setNodes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const alive = useRef(true);
  const inFlight = useRef(false);
  const load = async () => {
    setLoading(true); setError('');
    try {
      const data = await adminRequest<{ policy: PlacementPolicy; nodes: string[] }>('/api/admin/placement-policy');
      if (alive.current) { setSaved(data.policy); setDraft(data.policy); setNodes(data.nodes); }
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : '读取失败'); }
    finally { if (alive.current) setLoading(false); }
  };
  useEffect(() => {
    alive.current = true;
    const timer = window.setTimeout(() => { void load(); }, 0);
    return () => { alive.current = false; window.clearTimeout(timer); };
  }, []);
  const save = async () => {
    if (!saved || inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(''); setNotice('');
    try {
      const data = await adminRequest<{ policy: PlacementPolicy }>('/api/admin/placement-policy', {
        method: 'POST', body: JSON.stringify({ policy: draft, expected: saved }),
      });
      if (alive.current) { setSaved(data.policy); setDraft(data.policy); setNotice('已保存，仅影响之后新建的实例。'); onSaved(); }
    } catch (e) { if (alive.current) setError(`${e instanceof Error ? e.message : '保存失败'}；请重新加载核对后再保存。`); }
    finally { inFlight.current = false; if (alive.current) setBusy(false); }
  };
  return <section className="rental-card rental-registration-settings rental-placement-settings" aria-labelledby="placement-title">
    <div className="rental-card-head"><div><div className="rental-kicker">ADMIN / SCHEDULING</div><h2 id="placement-title">新实例优先节点</h2></div><span className="rental-step-chip">{loading ? '读取中…' : saved?.enabled ? `优先 ${saved.preferredNode}` : '自由选择'}</span></div>
    <p className="admin-help">开启后，客户默认只能使用优先节点。仅当该节点GPU已占满、不能提供所选卡数（四卡需完整组）、存储不足或离线时，才开放其他节点。现有实例不会搬迁。</p>
    <form className="rental-registration-form" onSubmit={event => { event.preventDefault(); void save(); }}>
      <label className="rental-field"><span>调度规则</span><select aria-label="调度规则" value={draft.enabled ? 'preferred' : 'free'} disabled={loading || busy || !saved} onChange={event => setDraft({ ...draft, enabled: event.target.value === 'preferred' })}><option value="preferred">优先节点，满足例外时放行</option><option value="free">允许客户自由选择节点</option></select></label>
      <label className="rental-field"><span>优先节点</span><select aria-label="优先节点" value={draft.preferredNode} disabled={loading || busy || !saved} onChange={event => setDraft({ ...draft, preferredNode: event.target.value })}>{nodes.map(node => <option key={node} value={node}>{node}</option>)}</select></label>
      <button className="rental-small-button" disabled={loading || busy || !saved}>{busy ? '保存中…' : '保存调度规则'}</button>
      <button className="rental-small-button" type="button" disabled={loading || busy} onClick={() => void load()}>重新加载</button>
    </form>
    {!draft.enabled && <p className="admin-help">保存后将解除客户的优先节点限制。</p>}
    {error && <p className="admin-notice is-error" role="alert">{error}</p>}
    {notice && <output className="admin-notice">{notice}</output>}
  </section>;
}
