import cv2
from network.network import *
import airsim, time
import random
import matplotlib.pyplot as plt
from util.transformations import euler_from_quaternion
from network.loss_functions import *
from numpy import linalg as LA
from aux_functions import get_CustomImage, get_MonocularImageRGB, get_StereoImageRGB

class PedraAgent():
    def __init__(self, cfg, client, name, vehicle_name):
        self.iter = 0
        self.vehicle_name = vehicle_name
        self.client = client
        half_name = name.replace(vehicle_name, '')
        print('Initializing ', half_name)
        self.initialize_network(cfg, name)


    ###########################################################################
    # Drone related modules
    ###########################################################################

    def take_action(self, action, num_actions, SimMode):
        # Set Paramaters
        fov_v = (45 * np.pi / 180)/1.5
        fov_h = (80 * np.pi / 180)/1.5
        r = 0.4

        ignore_collision = False
        sqrt_num_actions = np.sqrt(num_actions)

        posit = self.client.simGetVehiclePose(vehicle_name=self.vehicle_name)
        pos = posit.position
        orientation = posit.orientation

        quat = (orientation.w_val, orientation.x_val, orientation.y_val, orientation.z_val)
        eulers = euler_from_quaternion(quat)
        alpha = eulers[2]

        theta_ind = int(action[0] / sqrt_num_actions)
        psi_ind = action[0] % sqrt_num_actions

        theta = fov_v/sqrt_num_actions * (theta_ind - (sqrt_num_actions - 1) / 2)
        psi = fov_h / sqrt_num_actions * (psi_ind - (sqrt_num_actions - 1) / 2)


        if SimMode == 'ComputerVision':
            noise_theta = (fov_v / sqrt_num_actions) / 6
            noise_psi = (fov_h / sqrt_num_actions) / 6

            psi = psi + random.uniform(-1, 1) * noise_psi
            theta = theta + random.uniform(-1, 1) * noise_theta

            x = pos.x_val + r * np.cos(alpha + psi)
            y = pos.y_val + r * np.sin(alpha + psi)
            z = pos.z_val + r * np.sin(theta)  # -ve because Unreal has -ve z direction going upwards

            self.client.simSetVehiclePose(airsim.Pose(airsim.Vector3r(x, y, z), airsim.to_quaternion(0, 0, alpha + psi)),
                                     ignore_collison=ignore_collision, vehicle_name=self.vehicle_name)
        elif SimMode == 'Multirotor':
            r_infer = 0.4
            vx = r_infer * np.cos(alpha + psi)
            vy = r_infer * np.sin(alpha + psi)
            vz = r_infer * np.sin(theta)
            # TODO
            # Take average of previous velocities and current to smoothen out drone movement.
            self.client.moveByVelocityAsync(vx=vx, vy=vy, vz=vz, duration=1,
                                       drivetrain=airsim.DrivetrainType.MaxDegreeOfFreedom,
                                       yaw_mode=airsim.YawMode(is_rate=False,
                                                               yaw_or_rate=180 * (alpha + psi) / np.pi), vehicle_name = self.vehicle_name)
            time.sleep(0.07)
            self.client.moveByVelocityAsync(vx=0, vy=0, vz=0, duration=1, vehicle_name=self.vehicle_name)
            # print("")
            # print("Throttle:", throttle)
            # print('Yaw:', yaw)

            # self.client.moveByAngleThrottleAsync(pitch=-0.015, roll=0, throttle=throttle, yaw_rate=yaw, duration=0.2).join()
            # self.client.moveByVelocityAsync(vx=0, vy=0, vz=0, duration=0.005)

    def get_CustomDepth(self, cfg):
        camera_name = 2
        if cfg.env_type == 'indoor' or cfg.env_type == 'Indoor':
            max_tries = 5
            tries = 0
            correct = False
            while not correct or tries == max_tries:
                tries += 1
                responses = self.client.simGetImages(
                    [airsim.ImageRequest(camera_name, airsim.ImageType.DepthVis, False, False)],
                    vehicle_name=self.vehicle_name)
                img1d = np.fromstring(responses[0].image_data_uint8, dtype=np.uint8)
                # AirSim bug: Sometimes it returns invalid depth map with a few 255 and all 0s
                if np.max(img1d)==255 and np.mean(img1d)<0.05:
                    correct = False
                else:
                    correct = True
            depth = img1d.reshape(responses[0].height, responses[0].width, 3)[:, :, 0]
            thresh = 50
        elif cfg.env_type == 'outdoor' or cfg.env_type == 'Outdoor':
            responses = self.client.simGetImages([airsim.ImageRequest(1, airsim.ImageType.DepthPlanner, True)],
                                                 vehicle_name=self.vehicle_name)
            depth = airsim.list_to_2d_float_array(responses[0].image_data_float, responses[0].width, responses[0].height)
            thresh = 50

        # To make sure the wall leaks in the unreal environment doesn't mess up with the reward function
        super_threshold_indices = depth > thresh
        depth[super_threshold_indices] = thresh
        depth = depth / thresh

        return depth, thresh

    def get_state(self):

        camera_image = get_MonocularImageRGB(self.client, self.vehicle_name)
        self.iter = self.iter + 1
        state = cv2.resize(camera_image, (self.input_size, self.input_size), cv2.INTER_LINEAR)
        state = cv2.normalize(state, state, 0, 1, cv2.NORM_MINMAX, cv2.CV_32F)
        state_rgb = []
        state_rgb.append(state[:, :, 0:3])
        state_rgb = np.array(state_rgb)
        state_rgb = state_rgb.astype('float32')

        return state_rgb

    def GetAgentState(self):
        return self.client.simGetCollisionInfo(vehicle_name=self.vehicle_name)

    ###########################################################################
    # RL related modules
    ###########################################################################

    def avg_depth(self, depth_map1, thresh, debug, cfg):
        # Version 0.3 - NAN issue resolved
        # Thresholded depth map to ignore objects too far and give them a constant value
        # Globally (not locally as in the version 0.1) Normalise the thresholded map between 0 and 1
        # Threshold depends on the environment nature (indoor/ outdoor)
        depth_map = depth_map1
        global_depth = np.mean(depth_map)
        n = max(global_depth * thresh / 3, 1)
        H = np.size(depth_map, 0)
        W = np.size(depth_map, 1)
        grid_size = (np.array([H, W]) / n)

        # scale by 0.9 to select the window towards top from the mid line
        h = max(int(0.9 * H * (n - 1) / (2 * n)), 0)
        w = max(int(W * (n - 1) / (2 * n)), 0)
        grid_location = [h, w]

        x_start = int(round(grid_location[0]))
        y_start_center = int(round(grid_location[1]))
        x_end = int(round(grid_location[0] + grid_size[0]))
        y_start_right = min(int(round(grid_location[1] + grid_size[1])), W)
        y_start_left = max(int(round(grid_location[1] - grid_size[1])), 0)
        y_end_right = min(int(round(grid_location[1] + 2 * grid_size[1])), W)

        fract_min = 0.05

        L_map = depth_map[x_start:x_end, y_start_left:y_start_center]
        C_map = depth_map[x_start:x_end, y_start_center:y_start_right]
        R_map = depth_map[x_start:x_end, y_start_right:y_end_right]

        if not L_map.any():
            L1 = 0
        else:
            L_sort = np.sort(L_map.flatten())
            end_ind = int(np.round(fract_min * len(L_sort)))
            L1 = np.mean(L_sort[0:end_ind])

        if not R_map.any():
            R1 = 0
        else:
            R_sort = np.sort(R_map.flatten())
            end_ind = int(np.round(fract_min * len(R_sort)))
            R1 = np.mean(R_sort[0:end_ind])

        if not C_map.any():
            C1 = 0
        else:
            C_sort = np.sort(C_map.flatten())
            end_ind = int(np.round(fract_min * len(C_sort)))
            C1 = np.mean(C_sort[0:end_ind])

        if debug:
            cv2.rectangle(depth_map1, (y_start_center, x_start), (y_start_right, x_end), (0, 0, 0), 3)
            cv2.rectangle(depth_map1, (y_start_left, x_start), (y_start_center, x_end), (0, 0, 0), 3)
            cv2.rectangle(depth_map1, (y_start_right, x_start), (y_end_right, x_end), (0, 0, 0), 3)

            dispL = str(np.round(L1, 3))
            dispC = str(np.round(C1, 3))
            dispR = str(np.round(R1, 3))
            cv2.putText(depth_map1, dispL, (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), thickness=2)
            cv2.putText(depth_map1, dispC, (int(W / 2 - 40), 70), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), thickness=2)
            cv2.putText(depth_map1, dispR, (int(W - 80), 70), cv2.FONT_HERSHEY_COMPLEX, 1, (0, 0, 0), thickness=2)
            cmap = plt.get_cmap('jet')
            depth_map_heat = cmap(depth_map1)
            cv2.imshow('Depth Map: ' + self.vehicle_name, depth_map_heat)
            cv2.waitKey(1)

        return L1, C1, R1

    def reward_gen(self, d_new, action, crash_threshold, thresh, debug, cfg):
        L_new, C_new, R_new = self.avg_depth(d_new, thresh, debug, cfg)
        # For now, lets keep the reward a simple one
        if C_new < crash_threshold:
            done = True
            reward = -1
        else:
            done = False
            if action == 0:
                reward = C_new
            else:
                # reward = C_new/3
                reward = C_new

        return reward, done

    ###########################################################################
    # Network related modules
    ###########################################################################

    def initialize_network(self, cfg, name):
        self.g = tf.Graph()

        self.first_frame = True
        self.last_frame = []
        with self.g.as_default():
            stat_writer_path = cfg.network_path + self.vehicle_name + '/return_plot/'
            loss_writer_path = cfg.network_path + self.vehicle_name + '/loss' + name + '/'
            self.stat_writer = tf.summary.FileWriter(stat_writer_path)
            # name_array = 'D:/train/loss'+'/'+name
            self.loss_writer = tf.summary.FileWriter(loss_writer_path)
            self.env_type = cfg.env_type
            self.input_size = cfg.input_size
            self.num_actions = cfg.num_actions

            # Placeholders
            self.batch_size = tf.placeholder(tf.int32, shape=())
            self.learning_rate = tf.placeholder(tf.float32, shape=())
            self.X1 = tf.placeholder(tf.float32, [None, cfg.input_size, cfg.input_size, 3], name='States')

            # self.X = tf.image.resize_images(self.X1, (227, 227))

            self.X = tf.map_fn(lambda frame: tf.image.per_image_standardization(frame), self.X1)
            self.target = tf.placeholder(tf.float32, shape=[None], name='Qvals')
            self.actions = tf.placeholder(tf.int32, shape=[None], name='Actions')

            # self.model = AlexNetDuel(self.X, cfg.num_actions, cfg.train_fc)
            self.model = C3F2(self.X, cfg.num_actions, cfg.train_fc)

            self.predict = self.model.output
            ind = tf.one_hot(self.actions, cfg.num_actions)
            pred_Q = tf.reduce_sum(tf.multiply(self.model.output, ind), axis=1)
            self.loss = huber_loss(pred_Q, self.target)
            self.train = tf.train.AdamOptimizer(learning_rate=self.learning_rate, beta1=0.9, beta2=0.99).minimize(
                self.loss, name="train")

            self.sess = tf.InteractiveSession()
            tf.global_variables_initializer().run()
            tf.local_variables_initializer().run()
            self.saver = tf.train.Saver()
            self.all_vars = tf.trainable_variables()

            self.sess.graph.finalize()

        # Load custom weights from custom_load_path if required
        if cfg.custom_load:
            print('Loading weights from: ', cfg.custom_load_path)
            self.load_network(cfg.custom_load_path)

    def get_vars(self):
        return self.sess.run(self.all_vars)

    def initialize_graphs_with_average(self, agent, agent_on_same_network):

        values = {}
        var = {}
        all_assign = {}
        for name_agent in agent_on_same_network:
            values[name_agent] = agent[name_agent].get_vars()
            var[name_agent] = agent[name_agent].all_vars
            all_assign[name_agent] = []

        for i in range(len(values[name_agent])):
            val = []
            for name_agent in agent_on_same_network:
                val.append(values[name_agent][i])
            # Take mean here
            mean_val = np.average(val, axis=0)
            for name_agent in agent_on_same_network:
                # all_assign[name_agent].append(tf.assign(var[name_agent][i], mean_val))
                var[name_agent][i].load(mean_val, agent[name_agent].sess)


    def Q_val(self, xs):
        target = np.zeros(shape=[xs.shape[0]], dtype=np.float32)
        actions = np.zeros(dtype=int, shape=[xs.shape[0]])
        return self.sess.run(self.predict,
                      feed_dict={self.batch_size: xs.shape[0], self.learning_rate: 0, self.X1: xs,
                                 self.target: target, self.actions:actions})


    def train_n(self, xs, ys,actions, batch_size, dropout_rate, lr, epsilon, iter):
        _, loss, Q = self.sess.run([self.train,self.loss, self.predict],
                      feed_dict={self.batch_size: batch_size, self.learning_rate: lr, self.X1: xs,
                                               self.target: ys, self.actions: actions})
        meanQ = np.mean(Q)
        maxQ = np.max(Q)
        # Log to tensorboard
        self.log_to_tensorboard(tag = 'Loss', group=self.vehicle_name, value=LA.norm(loss)/batch_size, index=iter)
        self.log_to_tensorboard(tag='Epsilon', group=self.vehicle_name, value=epsilon, index=iter)
        self.log_to_tensorboard(tag='Learning Rate', group=self.vehicle_name, value=lr, index=iter)
        self.log_to_tensorboard(tag='MeanQ', group=self.vehicle_name, value=meanQ, index=iter)
        self.log_to_tensorboard(tag='MaxQ', group=self.vehicle_name, value=maxQ, index=iter)


    def action_selection(self, state):
        target = np.zeros(shape=[state.shape[0]], dtype=np.float32)
        actions = np.zeros(dtype=int, shape=[state.shape[0]])
        qvals= self.sess.run(self.predict,
                             feed_dict={self.batch_size: state.shape[0], self.learning_rate: 0.0001,
                                        self.X1: state,
                                        self.target: target, self.actions:actions})

        if qvals.shape[0]>1:
            # Evaluating batch
            action = np.argmax(qvals, axis=1)
        else:
            # Evaluating one sample
            action = np.zeros(1)
            action[0]=np.argmax(qvals)

        return action.astype(int)

    def log_to_tensorboard(self, tag,group, value, index):
        summary = tf.Summary()
        tag = group+'/'+tag
        summary.value.add(tag=tag, simple_value=value)
        self.stat_writer.add_summary(summary, index)

    def save_network(self, save_path, episode=''):
        save_path = save_path + self.vehicle_name + '/'+self.vehicle_name +'_'+str(episode)
        self.saver.save(self.sess, save_path)
        print('Model Saved: ', save_path)

    def load_network(self, load_path):
        self.saver.restore(self.sess, load_path)

    def get_weights(self):
        xs=np.zeros(shape=(32, 227,227,3))
        actions = np.zeros(dtype=int, shape=[xs.shape[0]])
        ys = np.zeros(shape=[xs.shape[0]], dtype=np.float32)
        return self.sess.run(self.weights,
                             feed_dict={self.batch_size: xs.shape[0],  self.learning_rate: 0,
                                        self.X1: xs,
                                        self.target: ys, self.actions:actions})








