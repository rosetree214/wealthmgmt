import express from 'express';
import cors from 'cors';
import dotenv from 'dotenv';

import authRouter from './routes/auth';
import userRouter from './routes/user';
import portfolioRouter from './routes/portfolio';
import aiInsightsRouter from './routes/aiInsights';

// Load env vars
dotenv.config();

const PORT = process.env.PORT || 4000;
const app = express();

app.use(cors());
app.use(express.json());

// API Routes
app.use('/api/auth', authRouter);
app.use('/api/user', userRouter);
app.use('/api/portfolio', portfolioRouter);
app.use('/api/insights', aiInsightsRouter);

app.get('/health', (_, res) => res.json({ status: 'ok' }));

app.listen(PORT, () => {
  console.log(`WealthWise API running on http://localhost:${PORT}`);
});