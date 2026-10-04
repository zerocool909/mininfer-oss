import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {/* Root boundary: React unmounts the tree when a render throws, so without
        this a single bad field in the API payload leaves a blank white page. */}
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)
