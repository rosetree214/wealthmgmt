import { Router } from 'express';

const router = Router();

router.get('/', async (req, res) => {
  // TODO: fetch portfolio from prisma
  res.json({ portfolio: [] });
});

router.post('/refresh', async (req, res) => {
  // TODO: trigger Plaid sync / external data ingest
  res.json({ ok: true });
});

export default router;