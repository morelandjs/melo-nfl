"""Trains model and exposes predictor class objects"""

from datetime import datetime
import logging
import operator
import pickle

from hyperopt import fmin, hp, tpe, Trials
import matplotlib.pyplot as plt
from melo import Melo
import numpy as np
import pandas as pd

from .data import load_games
from . import cachedir


class MeloNFL(Melo):
    """
    Generate NFL point-spread or point-total predictions
    using the Margin-dependent Elo (MELO) model.

    """
    def __init__(self, mode, kfactor, regress_coeff,
                 rest_bonus, exp_bonus, weight_qb):

        # hyperparameters
        self.mode = mode
        self.kfactor = kfactor
        self.regress_coeff = regress_coeff
        self.rest_bonus = rest_bonus
        self.exp_bonus = exp_bonus
        self.weight_qb = weight_qb

        # model operation mode: 'spread' or 'total'
        if self.mode not in ['spread', 'total']:
            raise ValueError(
                "Unknown mode; valid options are 'spread' and 'total'")

        # mode-specific training hyperparameters
        self.commutes, self.compare, self.lines = {
            'total': (True, operator.add, np.arange(-0.5, 101.5)),
            'spread': (False, operator.sub, np.arange(-59.5, 60.5)),
        }[mode]

        # pre-process training data
        self.games = self.format_gamedata(load_games(update=False))
        self.teams = np.union1d(self.games.team_home, self.games.team_away)
        self.qbs = np.union1d(self.games.qb_home, self.games.qb_away)

        # train the model
        self.rms_error = self.train()

    def regress(self, months):
        """
        Regress ratings to the mean as a function of elapsed time.

        Regression fraction equals self.regress_coeff if months > 3, else 0.

        """
        return self.regress_coeff if months > 3 else 0

    def bias(self, games):
        """
        Circumstantial bias correction factor for each game.

        The bias factor includes two terms: a rest factor which compares the rest
        of each team, and an experience factor which compares the experience of
        each quarterback.

        """
        rest_bias = self.rest_bonus * self.compare(
            games.rested_home.astype(int),
            games.rested_away.astype(int),
        )

        exp_bias = self.exp_bonus * self.compare(
            -np.exp(-games.exp_home / 7.),
            -np.exp(-games.exp_away / 7.),

        )

        return rest_bias + exp_bias

    def combine(self, team_rating, qb_rating):
        """
        Combines team and quarterback ratings to form a single rating

        """
        return (1 - self.weight_qb)*team_rating + self.weight_qb*qb_rating

    def format_gamedata(self, games):
        """
        Preprocesses raw game data, returning a model input table.

        This function calculates some new columns and adds them to the
        games table:

             column  description
               home  home team name joined to home quarterback name
               away  away team name joined to away quarterback name
        rested_home  true if home team coming off bye week, false otherwise
        rested_away  true if away team coming off bye week, false otherwise
           exp_home  games played by the home quarterback
           exp_away  games played by the away quarterback

        """
        # sort games by date
        games = games.sort_values('datetime')

        # give jacksonville jaguars a single name
        games.replace('JAC', 'JAX', inplace=True)

        # give teams which haved moved cities their current name
        games.replace('SD', 'LAC', inplace=True)
        games.replace('STL', 'LA', inplace=True)

        # game dates for every team
        game_dates = pd.concat([
            games[['datetime', 'team_home']].rename(
                columns={'team_home': 'team'}),
            games[['datetime', 'team_away']].rename(
                columns={'team_away': 'team'}),
        ]).sort_values('datetime')

        # create home and away label columns
        games['home'] = games['team_home'] + '-' + games['qb_home']
        games['away'] = games['team_away'] + '-' + games['qb_away']

        # game dates for every team
        game_dates = pd.concat([
            games[['datetime', 'team_home']].rename(
                columns={'team_home': 'team'}),
            games[['datetime', 'team_away']].rename(
                columns={'team_away': 'team'}),
        ]).sort_values('datetime')

        # compute days rested
        for team in ['home', 'away']:
            games_prev = game_dates.rename(
                columns={'team': 'team_{}'.format(team)})

            games_prev['date_{}_prev'.format(team)] = games.datetime

            games = pd.merge_asof(
                games, games_prev,
                on='datetime', by='team_{}'.format(team),
                allow_exact_matches=False
            )

        # true if team is comming off bye week, false otherwise
        ten_days = pd.Timedelta('10 days')
        games['rested_home'] = (games.datetime - games.date_home_prev) > ten_days
        games['rested_away'] = (games.datetime - games.date_away_prev) > ten_days

        # games played by each qb
        qb_home = games[['datetime', 'qb_home']].rename(columns={'qb_home': 'qb'})
        qb_away = games[['datetime', 'qb_away']].rename(columns={'qb_away': 'qb'})

        qb_exp = pd.concat([qb_home, qb_away]).sort_values('datetime')
        qb_exp['exp'] = qb_exp.groupby('qb').cumcount()

        for team in ['home', 'away']:
            games = games.merge(
                qb_exp.rename(columns={'qb': f'qb_{team}', 'exp': f'exp_{team}'}),
                on=['datetime', f'qb_{team}'],
            )

        return games

    def train(self):
        """
        Trains the Margin Elo (MELO) model on the historical game data.

        Returns the model's root-mean-square error.

        """
        # instantiate the Melo base class
        super(MeloNFL, self).__init__(
            self.kfactor, lines=self.lines, sigma=1.0,
            regress=self.regress, regress_unit='month',
            commutes=self.commutes, combine=self.combine)

        # train the model
        self.fit(
            self.games.datetime,
            self.games.home,
            self.games.away,
            self.compare(
                self.games.score_home,
                self.games.score_away,
            ),
            self.bias(self.games)
        )

        # compute mean absolute error for calibration
        residuals = self.residuals()

        return np.sqrt(np.square(residuals[256:]).mean())

    def visualize_hyperopt(mode, trials, parameters):
        """
        Visualize hyperopt loss minimization.

        """
        plotdir = cachedir / 'plots'

        if not plotdir.exists():
            plotdir.mkdir()

        fig, axes = plt.subplots(
            ncols=5, figsize=(12, 3), sharey=True)

        losses = trials.losses()

        for ax, (label, vals) in zip(axes.flat, trials.vals.items()):
            c = plt.cm.coolwarm(np.linspace(0, 1, len(vals)))
            ax.scatter(vals, losses, c=c)
            ax.axvline(parameters[label], color='k')
            ax.set_xlabel(label)

            if ax.is_first_col():
                ax.set_ylabel('Mean absolute error')

        plotfile = plotdir / '{}_params.pdf'.format(mode)
        plt.tight_layout()
        plt.savefig(str(plotfile))

    def rank(self, time, statistic='mean'):
        """
        Modify melo ranking function to only consider teams (ignore qbs).

        """
        return super(MeloNFL, self).rank(
            time, labels=self.teams, statistic=statistic)

    @classmethod
    def from_cache(cls, mode, steps=100, calibrate=False):
        """
        Optimizes the MeloNFL model hyper parameters. Returns cached values
        if calibrate is False and the parameters are cached, otherwise it
        optimizes the parameters and saves them to the cache.

        """
        cachefile = cachedir / '{}.pkl'.format(mode)

        if not calibrate and cachefile.exists():
            with cachefile.open(mode='rb') as f:
                return pickle.load(f)

        def objective(params):
            return cls(mode, *params).rms_error

        limits = {
            'spread': [
                ('kfactor',       0.1, 0.4),
                ('regress_coeff', 0.1, 0.5),
                ('rest_bonus',    0.0, 0.2),
                ('exp_bonus',     0.0, 0.5),
                ('weight_qb',     0.0, 1.0),
            ],
            'total': [
                ('kfactor',       0.0, 0.3),
                ('regress_coeff', 0.1, 0.5),
                ('rest_bonus',    0.0, 0.2),
                ('exp_bonus',     0.0, 0.5),
                ('weight_qb',     0.0, 1.0),
            ]
        }

        space = [hp.uniform(*lim) for lim in limits[mode]]

        trials = Trials()

        logging.info('calibrating {} hyperparameters'.format(mode))

        load_games(update=True)

        parameters = fmin(objective, space, algo=tpe.suggest, max_evals=steps,
                      trials=trials, show_progressbar=False)

        model = cls(mode, **parameters)

        cls.visualize_hyperopt(mode, trials, parameters)

        cachefile.parent.mkdir(exist_ok=True)

        with cachefile.open(mode='wb') as f:
            logging.info('caching {} model to {}'.format(mode, cachefile))
            pickle.dump(model, f, protocol=pickle.HIGHEST_PROTOCOL)

        return model
