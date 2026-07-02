import { createApp } from 'vue'
import App from './App.vue'
import './styles/main.css'

try {
  createApp(App).mount('#app')
} catch (error) {
  console.error(error)
  const app = document.querySelector('#app')
  if (app) {
    app.innerHTML = `
      <main class="boot-shell">
        <header>
          <h1>AI GIS Agent</h1>
          <p>前端加载失败</p>
        </header>
        <section>
          <p>${error instanceof Error ? error.message : String(error)}</p>
        </section>
      </main>
    `
  }
}
