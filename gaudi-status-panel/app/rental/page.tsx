import { RentalPanel } from '@/components/rental/rental-panel';

export const metadata = { title: '1CatDL · 算力工作台', description: 'Gaudi2 多节点实例、余额账单与客户管理' };

export const dynamic = 'force-dynamic';

export default function RentalPage() {
  return <RentalPanel />;
}
