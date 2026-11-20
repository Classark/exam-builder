import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { applyStoredTheme } from './Appearance.tsx'
import { ErrorBoundary, installAppFeel } from './errors.tsx'
import { LiveProvider } from './live.tsx'
import { installPinchZoom } from './pinchZoom.ts'

applyStoredTheme()
installPinchZoom()
installAppFeel()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <LiveProvider>
        <App />
      </LiveProvider>
    </ErrorBoundary>
  </StrictMode>,
)
