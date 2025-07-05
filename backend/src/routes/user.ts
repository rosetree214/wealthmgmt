import { Router, Request, Response } from 'express';
import prisma from '../prisma';

const router = Router();

router.get('/profile', async (req, res) => {
  // TODO: auth middleware, get user info from prisma
  res.json({ user: { id: 1, name: 'Placeholder' } });
});

router.post('/profile', async (req: Request, res: Response) => {
  try {
    // TODO: authenticate user properly – for now assume userId = 1
    const userId = 1;
    const { tier, riskTolerance, liquidityNeeds, timeHorizon, values } = req.body;

    const updated = await prisma.user.update({
      where: { id: userId },
      data: {
        tier,
        profile: {
          riskTolerance,
          liquidityNeeds,
          timeHorizon,
          values,
        },
      }
    });
    res.json({ user: updated });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to update profile' });
  }
});

export default router;