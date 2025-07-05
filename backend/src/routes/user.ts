import { Router } from 'express';

const router = Router();

router.get('/profile', async (req, res) => {
  // TODO: auth middleware, get user info from prisma
  res.json({ user: { id: 1, name: 'Placeholder' } });
});

export default router;