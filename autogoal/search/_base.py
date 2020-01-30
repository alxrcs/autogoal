import enlighten
import warnings
import time
import datetime
import statistics
import math
import traceback
import sys

from autogoal.utils import ResourceManager, RestrictedWorkerByJoin, Min, Gb


class SearchAlgorithm:
    def __init__(
        self,
        generator_fn=None,
        fitness_fn=None,
        pop_size=1,
        maximize=True,
        errors="raise",
        early_stop=None,
        evaluation_timeout: int = 5 * Min,
        memory_limit: int = 4 * Gb,
        search_timeout: int = 60 * 60,
    ):
        if generator_fn is None and fitness_fn is None:
            raise ValueError("You must provide either `generator_fn` or `fitness_fn`")

        self._generator_fn = generator_fn or self._build_sampler()
        self._fitness_fn = fitness_fn or (lambda x: x)
        self._pop_size = pop_size
        self._maximize = maximize
        self._errors = errors
        self._evaluation_timeout = evaluation_timeout
        self._memory_limit = memory_limit
        self._early_stop = early_stop
        self._search_timeout = search_timeout

        if self._evaluation_timeout > 0 or self._memory_limit > 0:
            self._fitness_fn = RestrictedWorkerByJoin(
                self._fitness_fn, self._evaluation_timeout, self._memory_limit
            )

    def run(self, evaluations=None, logger=None):
        """Runs the search performing at most `evaluations` of `fitness_fn`.

        Returns:
            Tuple `(best, fn)` of the best found solution and its corresponding fitness.
        """
        if logger is None:
            logger = Logger()

        if evaluations is None:
            evaluations = math.inf

        if isinstance(logger, list):
            logger = MultiLogger(*logger)

        best_solution = None
        best_fn = None
        no_improvement = 0

        logger.begin(evaluations)

        start_time = time.time()

        try:
            while evaluations > 0:
                stop = False

                logger.start_generation(evaluations, best_fn)
                self._start_generation()

                no_improvement += 1
                fns = []

                for _ in range(self._pop_size):
                    solution = None

                    try:
                        solution = self._generator_fn(self._build_sampler())
                    except Exception as e:
                        logger.error(
                            "Error while generating solution: %s" % e, solution
                        )
                        continue

                    try:
                        logger.sample_solution(solution)
                        fn = self._fitness_fn(solution)
                    except Exception as e:
                        fn = 0
                        logger.error(e, solution)

                        if self._errors == "raise":
                            logger.end(best_solution, best_fn)
                            raise

                    logger.eval_solution(solution, fn)
                    fns.append(fn)

                    if (
                        best_fn is None
                        or (fn > best_fn and self._maximize)
                        or (fn < best_fn and not self._maximize)
                    ):
                        logger.update_best(solution, fn, best_solution, best_fn)
                        best_solution = solution
                        best_fn = fn
                        no_improvement = 0

                    evaluations -= 1

                    if evaluations <= 0:
                        stop = True
                        break

                    spent_time = time.time() - start_time
                    print("Timeout:", self._search_timeout, spent_time)

                    if (
                        self._search_timeout
                        and spent_time > self._search_timeout
                    ):
                        print("(!) Stopping since time spent is %.2f" % (spent_time))
                        stop = True
                        break

                    if self._early_stop and no_improvement > self._early_stop:
                        print("(!) Stopping since no improvement for %i" % no_improvement)
                        stop = True
                        break

                logger.finish_generation(fns)
                self._finish_generation(fns)

                if stop:
                    break

        except KeyboardInterrupt:
            pass

        logger.end(best_solution, best_fn)
        return best_solution, best_fn

    def _build_sampler(self):
        raise NotImplementedError()

    def _start_generation(self):
        pass

    def _finish_generation(self, fns):
        pass


class Logger:
    def begin(self, evaluations):
        pass

    def end(self, best, best_fn):
        pass

    def start_generation(self, evaluations, best_fn):
        pass

    def finish_generation(self, fns):
        pass

    def sample_solution(self, solution):
        pass

    def eval_solution(self, solution, fitness):
        pass

    def error(self, e: Exception, solution):
        pass

    def update_best(self, new_best, new_fn, previous_best, previous_fn):
        pass


class ConsoleLogger(Logger):
    def begin(self, evaluations):
        print("Starting search: evaluations=%i" % evaluations)
        self.start_time = time.time()
        self.start_evaluations = evaluations

    def start_generation(self, evaluations, best_fn):
        current_time = time.time()
        elapsed = int(current_time - self.start_time)
        avg_time = elapsed / (self.start_evaluations - evaluations + 1)
        remaining = int(avg_time * evaluations)
        elapsed = datetime.timedelta(seconds=elapsed)
        remaining = datetime.timedelta(seconds=remaining)
        print(
            "New generation started: best_fn=%.3f, evaluations=%i, elapsed=%s, remaining=%s"
            % (best_fn or 0, evaluations, elapsed, remaining)
        )

    def error(self, e: Exception, solution):
        print("(!) Error evaluating pipeline: %r" % e)

    def end(self, best, best_fn):
        print("Search completed: best_fn=%.3f, best=\n%r" % (best_fn, best))

    def sample_solution(self, solution):
        print("Evaluating pipeline:\n%r" % solution)

    def eval_solution(self, solution, fitness):
        print("Fitness=%.3f" % fitness)

    def update_best(self, new_best, new_fn, previous_best, previous_fn):
        print(
            "Best solution: improved=%.3f, previous=%.3f" % (new_fn, previous_fn or 0)
        )


class ProgressLogger(Logger):
    def begin(self, evaluations):
        self.manager = enlighten.get_manager()
        self.total_counter = self.manager.counter(
            total=evaluations, unit="runs", leave=False, desc="Best: 0.000"
        )

    def sample_solution(self, solution):
        self.total_counter.update()

    def update_best(self, new_best, new_fn, *args):
        self.total_counter.desc = "Best: %.3f" % new_fn

    def end(self, *args):
        self.total_counter.close()
        self.manager.stop()


class MemoryLogger(Logger):
    def __init__(self):
        self.generation_best_fn = [0]
        self.generation_mean_fn = []

    def update_best(self, new_best, new_fn, previous_best, previous_fn):
        self.generation_best_fn[-1] = new_fn

    def finish_generation(self, fns):
        try:
            mean = statistics.mean(fns)
        except:
            mean = 0
        self.generation_mean_fn.append(mean)
        self.generation_best_fn.append(self.generation_best_fn[-1])


class MultiLogger(Logger):
    def __init__(self, *loggers):
        self.loggers = loggers

    def run(self, name, *args, **kwargs):
        for logger in self.loggers:
            getattr(logger, name)(*args, **kwargs)

    def begin(self, *args, **kwargs):
        self.run("begin", *args, **kwargs)

    def end(self, *args, **kwargs):
        self.run("end", *args, **kwargs)

    def start_generation(self, *args, **kwargs):
        self.run("start_generation", *args, **kwargs)

    def finish_generation(self, *args, **kwargs):
        self.run("finish_generation", *args, **kwargs)

    def sample_solution(self, *args, **kwargs):
        self.run("sample_solution", *args, **kwargs)

    def eval_solution(self, *args, **kwargs):
        self.run("eval_solution", *args, **kwargs)

    def error(self, *args, **kwargs):
        self.run("error", *args, **kwargs)

    def update_best(self, *args, **kwargs):
        self.run("update_best", *args, **kwargs)
