import torch
from numpy import datetime64, array
from torch import Tensor

from torch_kalman.design import Design
from torch_kalman.process import Season, FourierSeasonDynamic
from torch_kalman.process.processes.local_trend import LocalTrend
from torch_kalman.process.processes.season.fourier import TBATS

from torch_kalman.tests import TestCaseTK


class TestProcess(TestCaseTK):

    def test_fourier_season(self):
        season = FourierSeasonDynamic(id='season', seasonal_period=24, K=2, decay=False, season_start=False)
        season.add_measure('measure')
        design = Design(processes=[season], measures=['measure'])
        for_batch = design.for_batch(1, 24 * 2)

        positions = []
        state = torch.randn(5)
        for i in range(for_batch.num_timesteps):
            state = for_batch.F(i)[0].matmul(state)
            positions.append(round(state[-1].item() * 100) / 100.)

        self.assertListEqual(positions[0:24], positions[-24:])

    def test_tbats_season(self):
        K = 3
        season = TBATS(id='season', seasonal_period=24, K=K, decay=False, season_start=False)
        season.add_measure('measure')
        design = Design(processes=[season], measures=['measure'])
        for_batch = design.for_batch(1, 24 * 7)

        positions = []
        state = torch.randn(int(K * 2))
        for i in range(for_batch.num_timesteps):
            state = for_batch.F(i)[0].matmul(state)
            pos = for_batch.H(i)[0].matmul(state)
            positions.append(round(pos.item() * 100) / 100.)

        self.assertListEqual(positions[0:24], positions[-24:])
        # import matplotlib
        # matplotlib.use('TkAgg')
        # from matplotlib import pyplot as plt
        # plt.plot(positions)
        # plt.show()

    def test_velocity(self):
        # no decay:
        lt = LocalTrend(id='test', decay_velocity=False)
        lt.add_measure('measure')
        design = Design(processes=[lt], measures=['measure'])
        batch_vel = design.for_batch(2, 1)

        # check F:
        self.assertListEqual(list1=batch_vel.F(0)[0].tolist(), list2=[[1., 1.], [0., 1.]])
        state_mean = Tensor([[1.], [-.5]])
        for i in range(3):
            state_mean = torch.mm(batch_vel.F(0)[0], state_mean)
            self.assertEqual(state_mean[0].item(), 1 - .5 * (i + 1.))
            self.assertEqual(state_mean[1].item(), -.5)

        # with decay:
        lt = LocalTrend(id='test', decay_velocity=(.50, 1.00))
        lt.add_measure('measure')
        design = Design(processes=[lt], measures=['measure'])
        batch_vel = design.for_batch(2, 1)
        self.assertLess(batch_vel.F(0)[0][1, 1], 1.0)
        self.assertGreater(batch_vel.F(0)[0][1, 1], 0.5)
        decay = design.processes['test'].decayed_transitions['velocity'].get_value()
        self.assertEqual(decay, batch_vel.F(0)[0][1, 1])

        state_mean = Tensor([[0.], [1.0]])
        for i in range(3):
            state_mean = torch.mm(batch_vel.F(0)[0], state_mean)
            self.assertEqual(decay ** (i + 1), state_mean[1].item())

    def test_discrete_seasons(self):
        # test seasons without durations
        season = Season(id='day_of_week', seasonal_period=7, season_duration=1, season_start='2018-01-01',
                        dt_unit='D')
        season.add_measure('measure')

        # need to include start_datetimes since included above
        with self.assertRaises(ValueError) as cm:
            season.for_batch(1, 1)
        self.assertEqual(cm.exception.args[0], 'Must pass `start_datetimes` to process `day_of_week`.')

        design = Design(processes=[season], measures=['measure'])
        process_kwargs = {'day_of_week': {'start_datetimes': array([datetime64('2018-01-01')])}}
        batch_season = design.for_batch(1, 1,
                                        process_kwargs=process_kwargs)

        # test transitions manually:
        state_mean = torch.arange(0.0, 7.0)[:, None]
        state_mean[0] = -state_mean[1:].sum()
        for i in range(10):
            state_mean_last = state_mean
        state_mean = torch.mm(batch_season.F(0)[0], state_mean)
        self.assertTrue((state_mean[1:] == state_mean_last[:-1]).all())

        self.assertListEqual(batch_season.H(0)[0].tolist(), [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]])

        # TODO: test seasons w/durations