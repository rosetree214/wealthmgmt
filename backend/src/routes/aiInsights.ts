import { Router } from 'express';

const router = Router();

router.post('/generate', async (req, res) => {
  // TODO: call OpenAI + time-series models
  res.json({ insights: [] });
});

export default router;