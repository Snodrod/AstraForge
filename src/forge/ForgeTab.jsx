// Entry of the Forge tab for App.jsx. The wizard itself (ForgePanel and everything it
// imports, including forge.css and GLTFExporter) is loaded on demand as a separate chunk, so
// it does not weigh down the studio start. Only the tab icon and the tab-strip CSS load eagerly.
import React, { Suspense, lazy } from 'react';
import { Anvil, Loader2 } from 'lucide-react';
import { useT } from '../i18n/index.js';
import './forge-tabs.css';

/** Icon for the studio tab (App.jsx). */
export const ForgeIcon = Anvil;

const LazyForgePanel = lazy(() => import('./ForgePanel.jsx'));

function Loading() {
  const t = useT();
  return (
    <div className="forge-loading" role="status">
      <Loader2 size={14} className="spin" />
      {t('forge.tab.loading')}
    </div>
  );
}

export default function ForgeTab(props) {
  return (
    <Suspense fallback={<Loading />}>
      <LazyForgePanel {...props} />
    </Suspense>
  );
}
