import { defineConfig } from 'vite';
export default defineConfig({server:{host:'127.0.0.1',fs:{deny:['**/.env','**/.env.*','**/data/**','**/server/**','**/scripts/**','**/tests/**']}},build:{target:'es2022',chunkSizeWarningLimit:1100}});
