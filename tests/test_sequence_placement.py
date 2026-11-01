"""Pure lookahead planning; no images, Qt windows or live preferences."""
import math
import unittest

from desktop_pet.sequence_placement import plan_axis


class SequencePlacementTests(unittest.TestCase):
    def assert_feasible(self, path, times, lower, upper, speed):
        for value, low, high in zip(path, lower, upper):
            self.assertGreaterEqual(value, low-1e-6)
            self.assertLessEqual(value, high+1e-6)
        for first, second, start, end in zip(path, path[1:], times, times[1:]):
            self.assertLessEqual(abs(second-first), speed*(end-start)+1e-5)

    @staticmethod
    def direction_reversals(path):
        signs = [1 if b > a else -1 for a, b in zip(path, path[1:]) if abs(b-a) > 1e-6]
        return sum(a != b for a, b in zip(signs, signs[1:]))

    def test_future_clearance_is_anticipated_and_home_reached_by_last_frame(self):
        times=(0,.25,.5,.75,1)
        low=(-100,)*5
        high=(100,100,-50,100,100)
        path=plan_axis(times,low,high,0,start=0,end=0,speed=100)
        self.assertEqual(path[0],0)
        self.assertEqual(path[-1],0)
        self.assertLess(path[1],-20)
        self.assertLessEqual(path[2],-50)
        self.assertLess(path[3],-20)
        for a,b,ta,tb in zip(path,path[1:],times,times[1:]):
            self.assertLessEqual(abs(b-a),100*(tb-ta)+1e-5)

    def test_roomy_frames_keep_the_character_stationary(self):
        self.assertEqual(plan_axis((0,1,2),(-20,)*3,(40,)*3,12,start=12,end=12),(12,12,12))

    def test_opposite_edge_constraints_and_adaptive_rate_remain_feasible(self):
        times=(0,.1,.2,.3,.4)
        low=(-100,40,-100,-100,-100)
        high=(100,100,100,-40,100)
        path=plan_axis(times,low,high,0,start=0,end=0,speed=10)
        self.assertEqual(path[0],0)
        self.assertEqual(path[-1],0)
        for value,a,b in zip(path,low,high):
            self.assertGreaterEqual(value,a-1e-6)
            self.assertLessEqual(value,b+1e-6)

    def test_duplicate_bridge_timestamp_and_single_frame(self):
        path=plan_axis((0,.25,.25,.5),(-20,)*4,(20,)*4,3,start=3,end=3)
        self.assertEqual(path,(3,3,3,3))
        self.assertEqual(plan_axis((0,),(-5,),(5,),9,start=9,end=9),(5,))

    def test_invalid_or_impossible_metadata_is_rejected(self):
        for args in [((),(),(),0),((1,0),(0,0),(1,1),0),((0,),(2,),(1,),0),
                     ((math.nan,),(0,),(1,),0),((0,0),(0,10),(1,11),0)]:
            with self.subTest(args=args),self.assertRaises(ValueError):plan_axis(*args)

    def test_temporary_silhouette_gaps_do_not_trigger_repeated_home_trips(self):
        times = tuple(range(9))
        lower = (-100,)*9
        upper = (100,100,-20,100,100,-40,100,100,100)
        path = plan_axis(times, lower, upper, 0, start=0, end=0, speed=100)
        self.assertEqual(path[0], 0)
        self.assertEqual(path[-1], 0)
        self.assertEqual(min(path), -40)
        self.assertEqual(self.direction_reversals(path), 1)
        self.assertAlmostEqual(sum(abs(b-a) for a,b in zip(path,path[1:])), 80)
        self.assertLessEqual(max(path[2:6]), -20, "A gap in required clearance sent the pet home too early")
        self.assert_feasible(path, times, lower, upper, 100)

    def test_left_and_right_edges_share_the_same_single_excursion_behavior(self):
        times = tuple(range(9))
        upper = (100,100,-20,100,100,-40,100,100,100)
        left = plan_axis(times, (-100,)*9, upper, 0, start=0, end=0, speed=100)
        right = plan_axis(times, tuple(-v for v in upper), (100,)*9,
                          0, start=0, end=0, speed=100)
        self.assertEqual(right, tuple(-v for v in left))
        self.assertEqual(self.direction_reversals(right), 1)

    def test_single_excursion_keeps_rate_limits_at_irregular_and_duplicate_times(self):
        times = (0,.1,.2,.2,.3,.4,.5,.6,.7,.8,1.2)
        lower = (-100,)*len(times)
        upper = (100,100,-10,-10,100,100,-15,100,100,100,100)
        path = plan_axis(times, lower, upper, 0, start=0, end=0, speed=100)
        self.assertEqual(path[0], 0)
        self.assertEqual(path[-1], 0)
        self.assertEqual(path[2], path[3])
        self.assertEqual(self.direction_reversals(path), 1)
        self.assert_feasible(path, times, lower, upper, 100)

    def test_mandatory_opposite_edge_turns_are_not_removed(self):
        times = tuple(range(7))
        lower = (-100,30,-100,35,-100,-100,-100)
        upper = (100,100,-30,100,100,100,100)
        path = plan_axis(times, lower, upper, 0, start=0, end=0, speed=100)
        self.assertGreaterEqual(path[1], 30)
        self.assertLessEqual(path[2], -30)
        self.assertGreaterEqual(path[3], 35)
        self.assertGreater(self.direction_reversals(path), 1)
        self.assert_feasible(path, times, lower, upper, 100)

    def test_holding_clearance_yields_to_a_conflicting_local_boundary(self):
        times = tuple(range(7))
        lower = (-100,-100,-100,-10,-100,-100,-100)
        upper = (100,100,-20,100,-40,100,100)
        path = plan_axis(times, lower, upper, 0, start=0, end=0, speed=100)
        self.assertLessEqual(path[2], -20)
        self.assertGreaterEqual(path[3], -10, "A held clearance crossed the opposite boundary")
        self.assertLessEqual(path[4], -40)
        self.assertEqual(path[0], 0)
        self.assertEqual(path[-1], 0)
        self.assert_feasible(path, times, lower, upper, 100)

    def test_roomy_one_sided_excursion_accelerates_and_stops_gently(self):
        times = tuple(index / 10 for index in range(41))
        lower = (-100,) * len(times)
        upper = tuple(-20 if index == 20 else 100 for index in range(len(times)))
        path = plan_axis(times, lower, upper, 0, start=0, end=0, speed=100)
        velocities = [(b-a) / (tb-ta) for a, b, ta, tb in
                      zip(path, path[1:], times, times[1:])]
        self.assertEqual(path[0], 0)
        self.assertEqual(path[-1], 0)
        self.assertEqual(min(path), -20)
        self.assertLess(path[3], 0, "Departure waited for a sudden late correction")
        self.assertLess(abs(velocities[0]), 1)
        self.assertLess(abs(velocities[-1]), 1)
        self.assertLess(max(abs(b-a) for a, b in zip(velocities, velocities[1:])), 6)
        self.assert_feasible(path, times, lower, upper, 100)

    def test_actual_balloon_clearances_keep_peak_and_reduce_velocity_jumps(self):
        # Recorded worst-pose intervals, including adjacent-PNG intersection,
        # from the installed 1024px balloon at 200% in a 1707x1067 work area.
        # Translate home to zero. The conservative left limit is valid for
        # every source PNG, so this fixture needs no images or native Qt.
        upper = (
            10, 10, 10, 10, 11, 11, 11, 11, 11, 11, 11, 11, 11, 10,
            0, -9, -17, -25, -32, -37, -37, -37, -34, -32, -29, -26,
            -24, -21, -19, -17, -16, -34, -50, -64, -77, -90, -102,
            -114, -116, -116, -116, -116, -116, -116, -116, -116,
            -116, -116, -116, -116, -116, -116, -116, -116, -116,
            -116, 11, 11, 11, 11, 11, 11, 11, 11, 11, 10, 10, 10,
        )
        lower = (-1335,) * len(upper)
        times = tuple(index / 12 for index in range(len(upper)))
        path = plan_axis(times, lower, upper, 0, start=0, end=0, speed=240)
        velocities = [(b-a) / (tb-ta) for a, b, ta, tb in
                      zip(path, path[1:], times, times[1:])]
        self.assertEqual(path[0], 0)
        self.assertEqual(path[-1], 0)
        self.assertEqual(min(path), -116)
        self.assertEqual(self.direction_reversals(path), 1)
        self.assertLess(max(abs(b-a) for a, b in zip(velocities, velocities[1:])), 60,
                        "The old piecewise clearance jumped 240px/s at a PNG boundary")
        self.assertLess(abs(velocities[0]), 1)
        self.assertLess(abs(velocities[-1]), 8)
        self.assert_feasible(path, times, lower, upper, 240)

    def test_custom_pins_are_preserved_instead_of_being_eased_to_home(self):
        times = (0, 1, 2, 3, 4)
        lower = (-100,) * len(times)
        upper = (100, 100, -40, 100, 100)
        path = plan_axis(times, lower, upper, 0, start=-10, end=-5, speed=100)
        self.assertEqual(path[0], -10)
        self.assertEqual(path[-1], -5)
        self.assert_feasible(path, times, lower, upper, 100)
        for start, end in ((math.nan, 0), (0, math.inf)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                plan_axis(times, lower, upper, 0, start=start, end=end)

    def test_eased_duplicate_timestamps_share_one_exact_clamped_position(self):
        times = (0, 1, 1, 3, 4, 5)
        lower = (-100,) * len(times)
        upper = (100, -20, 100, -40, 100, 100)
        path = plan_axis(times, lower, upper, 0, start=0, end=0, speed=100)
        self.assertEqual(path[1], path[2], "Bridge and native nodes at one instant diverged")
        self.assertLessEqual(path[1], -20)
        self.assert_feasible(path, times, lower, upper, 100)


if __name__=='__main__':unittest.main()
