import { Router } from 'express';
import bcrypt from 'bcrypt';
import jwt from 'jsonwebtoken';

const router = Router();

// POST /register
router.post('/register', async (req, res) => {
  // TODO: validate body
  const { email, password } = req.body;
  const hashed = await bcrypt.hash(password, 10);
  // TODO: save user via Prisma
  // const user = await prisma.user.create({ data: { email, password: hashed } });
  res.json({ ok: true });
});

// POST /login
router.post('/login', async (req, res) => {
  const { email, password } = req.body;
  // TODO: find user via Prisma
  // const user = await prisma.user.findUnique({ where: { email } });
  const user = null;
  if (!user) return res.status(401).json({ error: 'Invalid creds' });
  const valid = await bcrypt.compare(password, user.password);
  if (!valid) return res.status(401).json({ error: 'Invalid creds' });
  const token = jwt.sign({ sub: user.id }, process.env.JWT_SECRET as string, { expiresIn: '1d' });
  res.json({ token });
});

export default router;