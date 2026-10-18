import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import { t } from './i18n/index.js';
import './style.css';
import './rig.css';
import './surface.css';
class ErrorBoundary extends React.Component{constructor(p){super(p);this.state={error:null};}static getDerivedStateFromError(error){return {error};}render(){return this.state.error?<div className="fatal"><h1>{t('main.error.title')}</h1><p>{this.state.error.message}</p><button onClick={()=>location.reload()}>{t('main.error.reload')}</button></div>:this.props.children;}}
createRoot(document.getElementById('root')).render(<ErrorBoundary><App/></ErrorBoundary>);
