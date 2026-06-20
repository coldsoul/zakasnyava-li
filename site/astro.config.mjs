import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://sofiaontime.com',
  base: '/',
  output: 'static',
  build: {
    format: 'directory',
  },
});
