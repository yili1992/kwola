#
#     Kwola is an AI algorithm that learns how to use other programs
#     automatically so that it can find bugs in them.
#
#     Copyright (C) 2020 Kwola Software Testing Inc.
#
#     This program is free software: you can redistribute it and/or modify
#     it under the terms of the GNU Affero General Public License as
#     published by the Free Software Foundation, either version 3 of the
#     License, or (at your option) any later version.
#
#     This program is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU Affero General Public License for more details.
#
#     You should have received a copy of the GNU Affero General Public License
#     along with this program.  If not, see <https://www.gnu.org/licenses/>.
#


from ...config.logger import getLogger, setupLocalLogging
from ...components.agents.DeepLearningAgent import DeepLearningAgent
from ...components.environments.WebEnvironment import WebEnvironment
from ...tasks.TaskProcess import TaskProcess
from ...config.config import KwolaCoreConfiguration
from ...datamodels.ExecutionSessionModel import ExecutionSession
from ...datamodels.ExecutionTraceModel import ExecutionTrace
from ...datamodels.TestingStepModel import TestingStep
from ...datamodels.TrainingStepModel import TrainingStep
from datetime import datetime
import atexit
import concurrent.futures
import gzip
import json
import billiard as multiprocessing
import billiard.pool as multiprocessingpool
import numpy
import os
import pickle
import random
import scipy.special
import sys
import tempfile
import time
import torch
import torch.distributed
import traceback
import google.api_core.exceptions
from google.cloud import storage


storageClient = storage.Client()


def isNumpyArray(obj):
    return type(obj).__module__ == numpy.__name__


class TrainingManager:
    def __init__(self, configDir, trainingSequenceId, trainingStepIndex, gpu=None, coordinatorTempFileName="kwola_distributed_coordinator", testingRunId=None, applicationId=None, gpuWorldSize=torch.cuda.device_count(), plugins=None):
        self.config = KwolaCoreConfiguration(configDir)
        self.configDir = configDir

        self.gpu = gpu
        self.coordinatorTempFileName = coordinatorTempFileName
        self.gpuWorldSize = gpuWorldSize
        self.trainingSequenceId = trainingSequenceId
        self.testingRunId = testingRunId
        self.applicationId = applicationId
        self.trainingStepIndex = trainingStepIndex

        self.trainingStep = None

        self.totalBatchesNeeded = self.config['iterations_per_training_step'] * self.config['batches_per_iteration'] + int(self.config['training_surplus_batches'])
        self.batchesPrepared = 0
        self.batchFutures = []
        self.recentCacheHits = []
        self.starved = False
        self.lastStarveStateAdjustment = 0
        self.coreLearningTimes = []

        self.testingSteps = []
        self.agent = None

        self.batchDirectory = None
        self.subProcessCommandQueues = []
        self.subProcessBatchResultQueues = []
        self.subProcesses = []

        if plugins is None:
            self.plugins = []
        else:
            self.plugins = plugins


    def createTrainingStep(self):
        trainingStep = TrainingStep(id=str(self.trainingSequenceId) + "_training_step_" + str(self.trainingStepIndex))
        trainingStep.startTime = datetime.now()
        trainingStep.trainingSequenceId = self.trainingSequenceId
        trainingStep.testingRunId = self.testingRunId
        trainingStep.applicationId = self.applicationId
        trainingStep.status = "running"
        trainingStep.numberOfIterationsCompleted = 0
        trainingStep.presentRewardLosses = []
        trainingStep.discountedFutureRewardLosses = []
        trainingStep.tracePredictionLosses = []
        trainingStep.executionFeaturesLosses = []
        trainingStep.predictedCursorLosses = []
        trainingStep.totalRewardLosses = []
        trainingStep.totalLosses = []
        trainingStep.totalRebalancedLosses = []
        trainingStep.hadNaN = False
        trainingStep.saveToDisk(self.config)
        self.trainingStep = trainingStep

    def initializeGPU(self):
        if self.gpu is not None:
            for subprocessIndex in range(10):
                try:
                    torch.distributed.init_process_group(backend="gloo",
                                                         world_size=self.gpuWorldSize,
                                                         rank=self.gpu,
                                                         init_method=f"file:///tmp/{self.coordinatorTempFileName}")
                    break
                except RuntimeError:
                    time.sleep(1)
                    if subprocessIndex == 9:
                        raise
            torch.cuda.set_device(self.gpu)
            getLogger().info(f"[{os.getpid()}] Cuda Ready on GPU {self.gpu}")

    def loadTestingSteps(self):
        self.testingSteps = [step for step in TrainingManager.loadAllTestingSteps(self.config, self.applicationId) if step.status == "completed"]


    def runTraining(self):
        success = True
        exception = None

        try:
            try:
                multiprocessing.set_start_method('spawn')
            except RuntimeError:
                pass
            getLogger().info(f"[{os.getpid()}] Starting Training Step")

            self.initializeGPU()
            self.createTrainingStep()
            self.loadTestingSteps()

            if len(self.testingSteps) == 0:
                errorMessage = f"Error, no test sequences to train on for training step."
                getLogger().warning(f"[{os.getpid()}] {errorMessage}")
                getLogger().info(f"[{os.getpid()}] ==== Training Step Completed ====")
                return {"success": False, "exception": errorMessage}

            self.agent = DeepLearningAgent(config=self.config, whichGpu=self.gpu)
            self.agent.initialize()
            self.agent.load()

            self.createSubproccesses()

            for plugin in self.plugins:
                plugin.trainingStepStarted(self.trainingStep)

        except Exception as e:
            errorMessage = f"Error occurred during initiation of training! {traceback.format_exc()}"
            getLogger().warning(f"[{os.getpid()}] {errorMessage}")
            return {"success": False, "exception": errorMessage}

        try:
            self.threadExecutor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.config['training_max_batch_prep_thread_workers'] * self.config['training_batch_prep_subprocesses'])

            self.queueBatchesForPrecomputation()

            while self.trainingStep.numberOfIterationsCompleted < self.config['iterations_per_training_step']:
                self.updateBatchPrepStarvedState()
                batches = self.fetchBatchesForIteration()

                success = self.learnFromBatches(batches)
                if not success:
                    break

                if self.trainingStep.numberOfIterationsCompleted % self.config['training_update_target_network_every'] == (self.config['training_update_target_network_every'] - 1):
                    getLogger().info(f"[{os.getpid()}] Updating the target network weights to the current primary network weights.")
                    self.agent.updateTargetNetwork()

                self.trainingStep.numberOfIterationsCompleted += 1

                if self.trainingStep.numberOfIterationsCompleted % self.config['print_loss_iterations'] == (self.config['print_loss_iterations'] - 1):
                    if self.gpu is None or self.gpu == 0:
                        timePerBatch = (datetime.now() - self.trainingStep.startTime).total_seconds() / self.trainingStep.numberOfIterationsCompleted
                        getLogger().info(f"[{os.getpid()}] Completed {self.trainingStep.numberOfIterationsCompleted + 1} batches. Average time per batch: {timePerBatch:.3f}. Core learning time: {numpy.average(self.coreLearningTimes):.3f}")
                        self.printMovingAverageLosses()
                        if self.config['print_cache_hit_rate']:
                            getLogger().info(f"[{os.getpid()}] Batch cache hit rate {100 * numpy.mean(self.recentCacheHits[-self.config['print_cache_hit_rate_moving_average_length']:]):.0f}%")

                if self.trainingStep.numberOfIterationsCompleted % self.config['iterations_between_db_saves'] == (self.config['iterations_between_db_saves'] - 1):
                    if self.gpu is None or self.gpu == 0:
                        self.trainingStep.saveToDisk(self.config)

                for plugin in self.plugins:
                    plugin.iterationCompleted(self.trainingStep)

            getLogger().info(f"[{os.getpid()}] Finished the core training loop. Saving the training step {self.trainingStep.id}")
            self.trainingStep.endTime = datetime.now()
            self.trainingStep.averageTimePerIteration = (self.trainingStep.endTime - self.trainingStep.startTime).total_seconds() / self.trainingStep.numberOfIterationsCompleted
            self.trainingStep.averageLoss = float(numpy.mean(self.trainingStep.totalLosses))
            self.trainingStep.status = "completed"

            for plugin in self.plugins:
                plugin.trainingStepFinished(self.trainingStep)

            self.trainingStep.saveToDisk(self.config)

            self.threadExecutor.shutdown(wait=True)

            self.shutdownAndJoinSubProcesses()
            self.saveAgent()

        except Exception:
            getLogger().error(f"[{os.getpid()}] Error occurred while learning sequence!\n{traceback.format_exc()}")
            success = False
            exception = traceback.format_exc()
        finally:
            files = os.listdir(self.batchDirectory)
            for file in files:
                os.unlink(os.path.join(self.batchDirectory, file))
            os.rmdir(self.batchDirectory)

            del self.agent

        # This print statement will trigger the parent manager process to kill this process.
        getLogger().info(f"[{os.getpid()}] ==== Training Step Completed ====")
        returnData = {"trainingStepId": str(self.trainingStep.id), "success": success}
        if exception is not None:
            returnData['exception'] = exception

        return returnData

    def learnFromBatches(self, batches):
        learningIterationStartTime = datetime.now()
        results = self.agent.learnFromBatches(batches)
        learningIterationFinishTime = datetime.now()
        self.coreLearningTimes.append((learningIterationFinishTime - learningIterationStartTime).total_seconds())

        if results is not None:
            for result, batch in zip(results, batches):
                totalRewardLoss, presentRewardLoss, discountedFutureRewardLoss, \
                stateValueLoss, advantageLoss, actionProbabilityLoss, tracePredictionLoss, \
                executionFeaturesLoss, predictedCursorLoss, \
                totalLoss, totalRebalancedLoss, batchReward, \
                sampleRewardLosses = result

                self.trainingStep.presentRewardLosses.append(presentRewardLoss)
                self.trainingStep.discountedFutureRewardLosses.append(discountedFutureRewardLoss)
                self.trainingStep.stateValueLosses.append(stateValueLoss)
                self.trainingStep.advantageLosses.append(advantageLoss)
                self.trainingStep.actionProbabilityLosses.append(actionProbabilityLoss)
                self.trainingStep.tracePredictionLosses.append(tracePredictionLoss)
                self.trainingStep.executionFeaturesLosses.append(executionFeaturesLoss)
                self.trainingStep.predictedCursorLosses.append(predictedCursorLoss)
                self.trainingStep.totalRewardLosses.append(totalRewardLoss)
                self.trainingStep.totalRebalancedLosses.append(totalRebalancedLoss)
                self.trainingStep.totalLosses.append(totalLoss)

                for executionTraceId, sampleRewardLoss in zip(batch['traceIds'], sampleRewardLosses):
                    for subProcessCommandQueue in self.subProcessCommandQueues:
                        subProcessCommandQueue.put(
                            ("update-loss", {"executionTraceId": executionTraceId, "sampleRewardLoss": sampleRewardLoss}))
            return True
        else:
            self.trainingStep.hadNaN = True
            return False

    def queueBatchesForPrecomputation(self):
        # First we chuck some batch requests into the queue.
        for n in range(self.config['training_precompute_batches_count']):
            subProcessIndex = (self.batchesPrepared % self.config['training_batch_prep_subprocesses'])
            self.batchFutures.append(
                self.threadExecutor.submit(TrainingManager.prepareAndLoadBatch,
                                           self.subProcessCommandQueues[subProcessIndex],
                                           self.subProcessBatchResultQueues[subProcessIndex]))
            self.batchesPrepared += 1

    def createSubproccesses(self):
        # Haven't decided yet whether we should force Kwola to always write to disc or spool in memory
        # using /tmp. The following lines switch between the two approaches
        # self.batchDirectory = tempfile.mkdtemp(dir=getKwolaUserDataDirectory("batches"))
        self.batchDirectory = tempfile.mkdtemp()

        self.subProcessCommandQueues = []
        self.subProcessBatchResultQueues = []
        self.subProcesses = []

        for subprocessIndex in range(self.config['training_batch_prep_subprocesses']):
            subProcessCommandQueue = multiprocessing.Queue()
            subProcessBatchResultQueue = multiprocessing.Queue()

            subProcess = multiprocessing.Process(target=TrainingManager.prepareAndLoadBatchesSubprocess, args=(self.configDir, self.batchDirectory, subProcessCommandQueue, subProcessBatchResultQueue, subprocessIndex, self.applicationId))
            subProcess.start()
            atexit.register(lambda: subProcess.terminate())

            self.subProcessCommandQueues.append(subProcessCommandQueue)
            self.subProcessBatchResultQueues.append(subProcessBatchResultQueue)
            self.subProcesses.append(subProcess)

        for queue in self.subProcessBatchResultQueues:
            readyState = queue.get()

            if readyState == "error":
                raise Exception("Error occurred during batch prep sub process initiation.")

    def countReadyBatches(self):
        ready = 0
        for future in self.batchFutures:
            if future.done():
                ready += 1
        return ready


    def updateBatchPrepStarvedState(self):
        if self.trainingStep.numberOfIterationsCompleted > (self.lastStarveStateAdjustment + self.config['training_min_batches_between_starve_state_adjustments']):
            ready = self.countReadyBatches()
            if ready < (self.config['training_precompute_batches_count'] / 4):
                if not self.starved:
                    for subProcessCommandQueue in self.subProcessCommandQueues:
                        subProcessCommandQueue.put(("starved", {}))
                    self.starved = True
                    getLogger().info(
                        f"[{os.getpid()}] GPU pipeline is starved for batches. Ready batches: {ready}. Switching to starved state.")
                    self.lastStarveStateAdjustment = self.trainingStep.numberOfIterationsCompleted
            else:
                if self.starved:
                    for subProcessCommandQueue in self.subProcessCommandQueues:
                        subProcessCommandQueue.put(("full", {}))
                    self.starved = False
                    getLogger().info(f"[{os.getpid()}] GPU pipeline is full of batches. Ready batches: {ready}. Switching to full state")
                    self.lastStarveStateAdjustment = self.trainingStep.numberOfIterationsCompleted

    def fetchBatchesForIteration(self):
        batches = []

        for batchIndex in range(self.config['batches_per_iteration']):
            chosenBatchIndex = 0
            found = False
            for futureIndex, future in enumerate(self.batchFutures):
                if future.done():
                    chosenBatchIndex = futureIndex
                    found = True
                    break

            batchFetchStartTime = datetime.now()
            batch, cacheHitRate = self.batchFutures.pop(chosenBatchIndex).result()
            batchFetchFinishTime = datetime.now()

            fetchTime = (batchFetchFinishTime - batchFetchStartTime).total_seconds()

            if not found and fetchTime > 0.5:
                getLogger().info(
                    f"[{os.getpid()}] I was starved waiting for a batch to be assembled. Waited: {fetchTime:.2f}")

            self.recentCacheHits.append(float(cacheHitRate))
            batches.append(batch)

            if self.batchesPrepared <= self.totalBatchesNeeded:
                # Request another session be prepared
                subProcessIndex = (self.batchesPrepared % self.config['training_batch_prep_subprocesses'])
                self.batchFutures.append(self.threadExecutor.submit(TrainingManager.prepareAndLoadBatch,
                                                                    self.subProcessCommandQueues[subProcessIndex],
                                                                    self.subProcessBatchResultQueues[subProcessIndex]))
                self.batchesPrepared += 1

        return batches


    def shutdownAndJoinSubProcesses(self):
        getLogger().info(f"[{os.getpid()}] Shutting down and joining the sub-processes")
        for subProcess, subProcessCommandQueue in zip(self.subProcesses, self.subProcessCommandQueues):
            subProcessCommandQueue.put(("quit", {}))
            subProcess.join(timeout=30)
            if subProcess.is_alive():
                # Use kill in python 3.7+, terminate in lower versions
                if hasattr(subProcess, 'kill'):
                    subProcess.kill()
                else:
                    subProcess.terminate()

    def saveAgent(self):
        # Safe guard, don't save the model if any nan's were detected
        if not self.trainingStep.hadNaN:
            if self.gpu is None or self.gpu == 0:
                getLogger().info(f"[{os.getpid()}] Saving the core training model.")
                self.agent.save()
                if self.config['training_save_model_checkpoints']:
                    self.agent.save(saveName=str(self.trainingStep.id))
                getLogger().info(f"[{os.getpid()}] Agent saved!")
        else:
            getLogger().error(f"[{os.getpid()}] ERROR! A NaN was detected in this models output. Not saving model.")

    @staticmethod
    def saveExecutionTraceWeightData(traceWeightData, configDir):
        config = KwolaCoreConfiguration(configDir)

        weightFile = os.path.join(config.getKwolaUserDataDirectory("execution_trace_weight_files"), traceWeightData['id'] + "-weight.json")

        saveData = {"weight": traceWeightData['weight']}

        with open(weightFile, "wt") as f:
            json.dump(saveData, f)

    @staticmethod
    def writeSingleExecutionTrace(traceBatch, sampleCacheDir):
        traceId = traceBatch['traceIds'][0]

        cacheFile = os.path.join(sampleCacheDir, traceId + "-sample.pickle.gz")

        pickleBytes = pickle.dumps(traceBatch)
        compressedPickleBytes = gzip.compress(pickleBytes)

        # getLogger().info(f"Writing batch cache file {cacheFile}")
        maxAttempts = 10
        for attempt in range(maxAttempts):
            try:
                with open(cacheFile, 'wb') as file:
                    file.write(compressedPickleBytes)
                return
            except OSError:
                time.sleep(1.5 ** attempt)
                continue

    @staticmethod
    def addExecutionSessionToSampleCache(executionSessionId, config):
        getLogger().info(f"Adding {executionSessionId} to the sample cache.")
        config.connectToMongoIfNeeded()
        maxAttempts = 10
        for attempt in range(maxAttempts):
            try:
                agent = DeepLearningAgent(config, whichGpu=None)

                sampleCacheDir = config.getKwolaUserDataDirectory("prepared_samples")

                executionSession = ExecutionSession.loadFromDisk(executionSessionId, config)

                batches = agent.prepareBatchesForExecutionSession(executionSession)

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    futures = []
                    for traceIndex, traceBatch in zip(range(len(executionSession.executionTraces) - 1), batches):
                        futures.append(executor.submit(TrainingManager.writeSingleExecutionTrace, traceBatch, sampleCacheDir))
                    for future in futures:
                        future.result()
                getLogger().info(f"Finished adding {executionSessionId} to the sample cache.")
                break
            except Exception as e:
                if attempt == (maxAttempts - 1):
                    getLogger().error(f"[{os.getpid()}] Error! Failed to prepare samples for execution session {executionSessionId}. Error was: {traceback.print_exc()}")
                    raise
                else:
                    getLogger().warning(f"[{os.getpid()}] Warning! Failed to prepare samples for execution session {executionSessionId}. Error was: {traceback.print_exc()}")

    @staticmethod
    def prepareBatchesForExecutionTrace(configDir, executionTraceId, executionSessionId, batchDirectory, applicationId):
        try:
            config = KwolaCoreConfiguration(configDir)

            agent = DeepLearningAgent(config, whichGpu=None)

            sampleCacheDir = config.getKwolaUserDataDirectory("prepared_samples", ensureExists=False)
            cacheFile = os.path.join(sampleCacheDir, executionTraceId + "-sample.pickle.gz")

            # Just for compatibility with the old naming scheme
            oldCacheFileName = os.path.join(sampleCacheDir, executionTraceId + ".pickle.gz")

            # applicationStorageBucket = storage.Bucket(storageClient, "kwola-testing-run-data-" + applicationId + "-cache")
            # blob = storage.Blob(os.path.join(executionTraceId + "-sample.pickle.gz"), applicationStorageBucket)

            try:
                with open(cacheFile, 'rb') as file:
                    sampleBatch = pickle.loads(gzip.decompress(file.read()))
                # sampleBatch = pickle.loads(gzip.decompress(blob.download_as_string()))
                cacheHit = True
            except FileNotFoundError:
                try:
                    with open(oldCacheFileName, 'rb') as file:
                        sampleBatch = pickle.loads(gzip.decompress(file.read()))
                    # sampleBatch = pickle.loads(gzip.decompress(blob.download_as_string()))
                    cacheHit = True
                except FileNotFoundError:
                    TrainingManager.addExecutionSessionToSampleCache(executionSessionId, config)
                    cacheHit = False
                    with open(cacheFile, 'rb') as file:
                        sampleBatch = pickle.loads(gzip.decompress(file.read()))

            imageWidth = sampleBatch['processedImages'].shape[3]
            imageHeight = sampleBatch['processedImages'].shape[2]

            # Calculate the crop positions for the main training image
            if config['training_enable_image_cropping']:
                randomXDisplacement = random.randint(-config['training_crop_center_random_x_displacement'], config['training_crop_center_random_x_displacement'])
                randomYDisplacement = random.randint(-config['training_crop_center_random_y_displacement'], config['training_crop_center_random_y_displacement'])

                cropLeft, cropTop, cropRight, cropBottom = agent.calculateTrainingCropPosition(sampleBatch['actionXs'][0] + randomXDisplacement, sampleBatch['actionYs'][0] + randomYDisplacement, imageWidth, imageHeight)
            else:
                cropLeft = 0
                cropRight = imageWidth
                cropTop = 0
                cropBottom = imageHeight

            # Calculate the crop positions for the next state image
            if config['training_enable_next_state_image_cropping']:
                nextStateCropCenterX = random.randint(10, imageWidth - 10)
                nextStateCropCenterY = random.randint(10, imageHeight - 10)

                nextStateCropLeft, nextStateCropTop, nextStateCropRight, nextStateCropBottom = agent.calculateTrainingCropPosition(nextStateCropCenterX, nextStateCropCenterY, imageWidth, imageHeight, nextStepCrop=True)
            else:
                nextStateCropLeft = 0
                nextStateCropRight = imageWidth
                nextStateCropTop = 0
                nextStateCropBottom = imageHeight

            # Crop all the input images and update the action x & action y
            # This is done at this step because the cropping is random
            # and thus you don't want to store the randomly cropped version
            # in the redis cache
            sampleBatch['processedImages'] = sampleBatch['processedImages'][:, :, cropTop:cropBottom, cropLeft:cropRight]
            sampleBatch['pixelActionMaps'] = sampleBatch['pixelActionMaps'][:, :, cropTop:cropBottom, cropLeft:cropRight]
            sampleBatch['rewardPixelMasks'] = sampleBatch['rewardPixelMasks'][:, cropTop:cropBottom, cropLeft:cropRight]
            sampleBatch['actionXs'] = sampleBatch['actionXs'] - cropLeft
            sampleBatch['actionYs'] = sampleBatch['actionYs'] - cropTop

            sampleBatch['nextProcessedImages'] = sampleBatch['nextProcessedImages'][:, :, nextStateCropTop:nextStateCropBottom, nextStateCropLeft:nextStateCropRight]
            sampleBatch['nextPixelActionMaps'] = sampleBatch['nextPixelActionMaps'][:, :, nextStateCropTop:nextStateCropBottom, nextStateCropLeft:nextStateCropRight]

            # Add augmentation to the processed images. This is done at this stage
            # so that we don't store the augmented version in the redis cache.
            # Instead, we want the pure version in the redis cache and create a
            # new augmentation every time we load it.
            processedImage = sampleBatch['processedImages'][0]
            augmentedImage = agent.augmentProcessedImageForTraining(processedImage)
            sampleBatch['processedImages'][0] = augmentedImage

            fileDescriptor, fileName = tempfile.mkstemp(".bin", dir=batchDirectory)

            with open(fileDescriptor, 'wb') as batchFile:
                pickle.dump(sampleBatch, batchFile)

            return fileName, cacheHit
        except Exception:
            getLogger().critical(traceback.format_exc())
            raise

    @staticmethod
    def prepareAndLoadSingleBatchForSubprocess(config, executionTraceWeightDatas, executionTraceWeightDataIdMap, batchDirectory, cacheFullState, processPool, subProcessCommandQueue, subProcessBatchResultQueue, applicationId):
        try:
            traceWeights = numpy.array([traceWeightData['weight'] for traceWeightData in executionTraceWeightDatas])

            traceWeights = numpy.minimum(config['training_trace_selection_maximum_weight'], traceWeights)
            traceWeights = numpy.maximum(config['training_trace_selection_minimum_weight'], traceWeights)

            if not cacheFullState:
                # We bias the random selection of the algorithm towards whatever
                # is at the one end of the list when we aren't in cache full state.
                # This just gives a bit of bias towards the algorithm to select
                # the same execution traces while the system is booting up
                # and gets the GPU to full speed sooner without requiring the cache to be
                # completely filled. This is helpful when cold starting a training run, such
                # as when doing R&D. it basically plays no role once you have a run going
                # for any length of time since the batch cache will fill up within
                # a single training step.
                traceWeights = traceWeights + numpy.arange(0, config['training_trace_selection_cache_not_full_state_one_side_bias'], len(traceWeights))

            traceProbabilities = scipy.special.softmax(traceWeights)
            traceIds = [trace['id'] for trace in executionTraceWeightDatas]

            chosenExecutionTraceIds = numpy.random.choice(traceIds, [config['batch_size']], p=traceProbabilities)

            futures = []
            for traceId in chosenExecutionTraceIds:
                traceWeightData = executionTraceWeightDataIdMap[str(traceId)]

                future = processPool.apply_async(TrainingManager.prepareBatchesForExecutionTrace, (config.configurationDirectory, str(traceId), str(traceWeightData['executionSessionId']), batchDirectory, applicationId))
                futures.append(future)

            cacheHits = []
            samples = []
            for future in futures:
                batchFilename, cacheHit = future.get()
                cacheHits.append(float(cacheHit))

                with open(batchFilename, 'rb') as batchFile:
                    sampleBatch = pickle.load(batchFile)
                    samples.append(sampleBatch)

                os.unlink(batchFilename)

            batch = {}
            for key in samples[0].keys():
                # We have to do something special here since they are not concatenated the normal way
                if key == "symbolIndexes" or key == 'symbolWeights' \
                        or key == "nextSymbolIndexes" or key == 'nextSymbolWeights' \
                        or key == "decayingFutureSymbolIndexes" or key == 'decayingFutureSymbolWeights':
                    batch[key] = numpy.concatenate([sample[key][0] for sample in samples], axis=0)

                    currentOffset = 0
                    offsets = []
                    for sample in samples:
                        offsets.append(currentOffset)
                        currentOffset += len(sample[key][0])

                    if 'next' in key:
                        batch['nextSymbolOffsets'] = numpy.array(offsets)
                    elif 'decaying' in key:
                        batch['decayingFutureSymbolOffsets'] = numpy.array(offsets)
                    else:
                        batch['symbolOffsets'] = numpy.array(offsets)
                else:
                    if isNumpyArray(samples[0][key]):
                        batch[key] = numpy.concatenate([sample[key] for sample in samples], axis=0)
                    else:
                        batch[key] = [sample[key][0] for sample in samples]

            cacheHitRate = numpy.mean(cacheHits)

            resultFileDescriptor, resultFileName = tempfile.mkstemp()
            with open(resultFileDescriptor, 'wb') as file:
                pickle.dump((batch, cacheHitRate), file)

            subProcessBatchResultQueue.put(resultFileName)

            return cacheHitRate
        except Exception:
            getLogger().error(f"prepareAndLoadSingleBatchForSubprocess failed! Putting a retry into the queue.\n{traceback.format_exc()}")
            subProcessCommandQueue.put(("batch", {}))
            return 1.0

    @staticmethod
    def loadAllTestingSteps(config, applicationId=None):
        testStepsDir = config.getKwolaUserDataDirectory("testing_steps")

        if config['data_serialization_method'] == 'mongo':
            return list(TestingStep.objects(applicationId=applicationId).no_dereference())
        else:
            testingSteps = []

            for fileName in os.listdir(testStepsDir):
                if ".lock" not in fileName:
                    stepId = fileName
                    stepId = stepId.replace(".json", "")
                    stepId = stepId.replace(".gz", "")
                    stepId = stepId.replace(".pickle", "")

                    testingSteps.append(TestingStep.loadFromDisk(stepId, config))

            return testingSteps

    @staticmethod
    def updateTraceRewardLoss(traceId, sampleRewardLoss, configDir):
        config = KwolaCoreConfiguration(configDir)
        trace = ExecutionTrace.loadFromDisk(traceId, config, omitLargeFields=False)
        trace.lastTrainingRewardLoss = sampleRewardLoss
        trace.saveToDisk(config)

    @staticmethod
    def prepareAndLoadBatchesSubprocess(configDir, batchDirectory, subProcessCommandQueue, subProcessBatchResultQueue, subprocessIndex=0, applicationId=None):
        try:
            setupLocalLogging()

            config = KwolaCoreConfiguration(configDir)

            getLogger().info(f"[{os.getpid()}] Starting initialization for batch preparation sub process.")

            testingSteps = sorted([step for step in TrainingManager.loadAllTestingSteps(config, applicationId) if step.status == "completed"], key=lambda step: step.startTime, reverse=True)
            testingSteps = list(testingSteps)[:int(config['training_number_of_recent_testing_sequences_to_use'])]

            if len(testingSteps) == 0:
                getLogger().warning(f"[{os.getpid()}] Error, no test sequences to train on for training step.")
                subProcessBatchResultQueue.put("error")
                return
            else:
                getLogger().info(f"[{os.getpid()}] Found {len(testingSteps)} total testing steps for this application.")

            # We use this mechanism to force parallel preloading of all the execution traces. Otherwise it just takes forever...
            executionSessionIds = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=int(config['training_max_initialization_workers'] / config['training_batch_prep_subprocesses'])) as executor:
                executionSessionFutures = []
                for testStepIndex, testStep in enumerate(testingSteps):
                    if testStepIndex % config['training_batch_prep_subprocesses'] == subprocessIndex:
                        for sessionId in testStep.executionSessions:
                            executionSessionIds.append(str(sessionId))
                            executionSessionFutures.append(executor.submit(TrainingManager.loadExecutionSession, sessionId, config))

                executionSessions = [future.result() for future in executionSessionFutures]

            getLogger().info(f"[{os.getpid()}] Found {len(executionSessionIds)} total execution sessions that can be learned.")

            getLogger().info(f"[{os.getpid()}] Starting loading of execution trace weight datas.")

            executionTraceWeightDatas = []
            executionTraceWeightDataIdMap = {}

            initialDataLoadProcessPool = multiprocessingpool.Pool(processes=int(config['training_max_initialization_workers'] / config['training_batch_prep_subprocesses']), initializer=setupLocalLogging)

            executionTraceFutures = []
            for session in executionSessions:
                for traceId in session.executionTraces[:-1]:
                    executionTraceFutures.append(initialDataLoadProcessPool.apply_async(TrainingManager.loadExecutionTraceWeightData, [traceId, session.id, configDir, applicationId]))

            completed = 0
            for traceFuture in executionTraceFutures:
                traceWeightData = pickle.loads(traceFuture.get(timeout=30))
                if traceWeightData is not None:
                    executionTraceWeightDatas.append(traceWeightData)
                    executionTraceWeightDataIdMap[str(traceWeightData['id'])] = traceWeightData
                completed += 1
                if completed % 1000 == 0:
                    getLogger().info(f"[{os.getpid()}] Finished loading {completed} execution trace weight datas.")

            initialDataLoadProcessPool.close()
            initialDataLoadProcessPool.join()
            del initialDataLoadProcessPool

            getLogger().info(f"[{os.getpid()}] Finished loading of weight datas for {len(executionTraceWeightDatas)} execution traces.")

            del testingSteps, executionSessionIds, executionSessionFutures, executionSessions, executionTraceFutures
            getLogger().info(f"[{os.getpid()}] Finished initialization for batch preparation sub process.")

            if len(executionTraceWeightDatas) == 0:
                subProcessBatchResultQueue.put("error")
                raise RuntimeError("There are no execution trace weight datas to process in the algorithm.")

            processPool = multiprocessingpool.Pool(processes=config['training_initial_batch_prep_workers'], initializer=setupLocalLogging)
            backgroundTraceSaveProcessPool = multiprocessingpool.Pool(processes=config['training_background_trace_save_workers'], initializer=setupLocalLogging)
            executionTraceSaveFutures = {}

            batchCount = 0
            cacheFullState = True

            lastProcessPool = None
            lastProcessPoolFutures = []
            currentProcessPoolFutures = []

            needToResetPool = False
            starved = False

            subProcessBatchResultQueue.put("ready")

            cacheRateFutures = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=config['training_max_batch_prep_thread_workers']) as threadExecutor:
                while True:
                    message, data = subProcessCommandQueue.get()
                    if message == "quit":
                        break
                    elif message == "starved":
                        starved = True
                        needToResetPool = True
                    elif message == "full":
                        starved = False
                        needToResetPool = True
                    elif message == "batch":
                        # See if we need to refresh the process pool. This is done to make sure resources get let go and to be able to switch between the smaller and larger process pool
                        # Depending on the cache hit rate
                        if batchCount % config['training_reset_workers_every_n_batches'] == (config['training_reset_workers_every_n_batches'] - 1):
                            needToResetPool = True

                        future = threadExecutor.submit(TrainingManager.prepareAndLoadSingleBatchForSubprocess, config, executionTraceWeightDatas, executionTraceWeightDataIdMap, batchDirectory, cacheFullState, processPool, subProcessCommandQueue,
                                                       subProcessBatchResultQueue, applicationId)
                        cacheRateFutures.append(future)
                        currentProcessPoolFutures.append(future)

                        batchCount += 1
                    elif message == "update-loss":
                        executionTraceId = data["executionTraceId"]
                        sampleRewardLoss = data["sampleRewardLoss"]
                        if executionTraceId in executionTraceWeightDataIdMap:
                            traceWeightData = executionTraceWeightDataIdMap[executionTraceId]

                            # We do this check here because saving execution traces is actually a pretty CPU heavy process,
                            # so we only want to do it if the loss has actually changed by a significant degree
                            differenceRatio = abs(traceWeightData['weight'] - sampleRewardLoss) / (traceWeightData['weight'] + 1e-6)
                            if differenceRatio > config['training_trace_selection_min_loss_ratio_difference_for_save']:
                                traceWeightData['weight'] = sampleRewardLoss
                                if executionTraceId not in executionTraceSaveFutures or executionTraceSaveFutures[executionTraceId].ready():
                                    traceSaveFuture = backgroundTraceSaveProcessPool.apply_async(TrainingManager.saveExecutionTraceWeightData, (traceWeightData, configDir))
                                    executionTraceSaveFutures[executionTraceId] = traceSaveFuture

                    if needToResetPool and lastProcessPool is None:
                        needToResetPool = False

                        cacheRates = [future.result() for future in cacheRateFutures[-config['training_cache_full_state_moving_average_length']:] if future.done()]

                        # If the cache is full and the main process isn't starved for batches, we shrink the process pool down to a smaller size.
                        if numpy.mean(cacheRates) > config['training_cache_full_state_min_cache_hit_rate'] and not starved:
                            lastProcessPool = processPool
                            lastProcessPoolFutures = list(currentProcessPoolFutures)
                            currentProcessPoolFutures = []

                            getLogger().debug(f"[{os.getpid()}] Resetting batch prep process pool. Cache full state. New workers: {config['training_cache_full_batch_prep_workers']}")

                            processPool = multiprocessingpool.Pool(processes=config['training_cache_full_batch_prep_workers'], initializer=setupLocalLogging)

                            cacheFullState = True
                        # Otherwise we have a full sized process pool so we can plow through all the results.
                        else:
                            lastProcessPool = processPool
                            lastProcessPoolFutures = list(currentProcessPoolFutures)
                            currentProcessPoolFutures = []

                            getLogger().debug(f"[{os.getpid()}] Resetting batch prep process pool. Cache starved state. New workers: {config['training_max_batch_prep_workers']}")

                            processPool = multiprocessingpool.Pool(processes=config['training_max_batch_prep_workers'], initializer=setupLocalLogging)

                            cacheFullState = False

                    if lastProcessPool is not None:
                        all = True
                        for future in lastProcessPoolFutures:
                            if not future.done():
                                all = False
                                break
                        if all:
                            lastProcessPool.terminate()
                            lastProcessPool = None
                            lastProcessPoolFutures = []

            backgroundTraceSaveProcessPool.close()
            backgroundTraceSaveProcessPool.join()
            processPool.terminate()
            if lastProcessPool is not None:
                lastProcessPool.terminate()

        except Exception:
            getLogger().error(f"[{os.getpid()}] Error occurred in the batch preparation sub-process. Exiting. {traceback.format_exc()}")

    @staticmethod
    def prepareAndLoadBatch(subProcessCommandQueue, subProcessBatchResultQueue):
        subProcessCommandQueue.put(("batch", {}))

        batchFileName = subProcessBatchResultQueue.get()
        with open(batchFileName, 'rb') as file:
            batch, cacheHit = pickle.load(file)
        os.unlink(batchFileName)

        return batch, cacheHit

    def printMovingAverageLosses(self):
        movingAverageLength = int(self.config['print_loss_moving_average_length'])

        averageStart = max(0, min(len(self.trainingStep.totalRewardLosses) - 1, movingAverageLength))

        averageTotalRewardLoss = numpy.mean(self.trainingStep.totalRewardLosses[-averageStart:])
        averagePresentRewardLoss = numpy.mean(self.trainingStep.presentRewardLosses[-averageStart:])
        averageDiscountedFutureRewardLoss = numpy.mean(self.trainingStep.discountedFutureRewardLosses[-averageStart:])

        averageStateValueLoss = numpy.mean(self.trainingStep.stateValueLosses[-averageStart:])
        averageAdvantageLoss = numpy.mean(self.trainingStep.advantageLosses[-averageStart:])
        averageActionProbabilityLoss = numpy.mean(self.trainingStep.actionProbabilityLosses[-averageStart:])

        averageTracePredictionLoss = numpy.mean(self.trainingStep.tracePredictionLosses[-averageStart:])
        averageExecutionFeatureLoss = numpy.mean(self.trainingStep.executionFeaturesLosses[-averageStart:])
        averagePredictedCursorLoss = numpy.mean(self.trainingStep.predictedCursorLosses[-averageStart:])
        averageTotalLoss = numpy.mean(self.trainingStep.totalLosses[-averageStart:])
        averageTotalRebalancedLoss = numpy.mean(self.trainingStep.totalRebalancedLosses[-averageStart:])

        message = f"[{os.getpid()}] "

        message += f"Moving Average Total Reward Loss: {averageTotalRewardLoss}\n"
        message += f"Moving Average Present Reward Loss: {averagePresentRewardLoss}\n"
        message += f"Moving Average Discounted Future Reward Loss: {averageDiscountedFutureRewardLoss}\n"
        message += f"Moving Average State Value Loss: {averageStateValueLoss}\n"
        message += f"Moving Average Advantage Loss: {averageAdvantageLoss}\n"
        message += f"Moving Average Action Probability Loss: {averageActionProbabilityLoss}\n"
        if self.config['enable_trace_prediction_loss']:
            message += f"Moving Average Trace Prediction Loss: {averageTracePredictionLoss}\n"
        if self.config['enable_execution_feature_prediction_loss']:
            message += f"Moving Average Execution Feature Loss: {averageExecutionFeatureLoss}\n"
        if self.config['enable_cursor_prediction_loss']:
            message += f"Moving Average Predicted Cursor Loss: {averagePredictedCursorLoss}\n"

        message += f"Moving Average Total Loss: {averageTotalLoss}\n"
        getLogger().info(message)

    @staticmethod
    def loadExecutionSession(sessionId, config):
        session = ExecutionSession.loadFromDisk(sessionId, config)
        if session is None:
            getLogger().error(f"[{os.getpid()}] Error! Did not find execution session {sessionId}")

        return session

    @staticmethod
    def loadExecutionTrace(traceId, configDir):
        config = KwolaCoreConfiguration(configDir)
        trace = ExecutionTrace.loadFromDisk(traceId, config, omitLargeFields=True)
        return pickle.dumps(trace)

    @staticmethod
    def loadExecutionTraceWeightData(traceId, sessionId, configDir, applicationId):
        try:
            config = KwolaCoreConfiguration(configDir)

            weightFile = os.path.join(config.getKwolaUserDataDirectory("execution_trace_weight_files"), traceId + "-weight.json")

            data = {}
            useDefault = False

            # applicationStorageBucket = storage.Bucket(storageClient, "kwola-testing-run-data-" + applicationId + "-cache")
            # blob = storage.Blob(os.path.join('execution_trace_weight_files', traceId + "-weight.json"), applicationStorageBucket)
            #
            # try:
            #     # startTime = datetime.now()
            #     data = json.loads(blob.download_as_string())
            #
            #     # finishTime = datetime.now()
            #     # getLogger().info((finishTime - startTime).total_seconds())
            #     useDefault = False
            # except google.api_core.exceptions.NotFound as e:
            #     useDefault = True
            # except Exception as e:
            #     getLogger().info(traceback.format_exc())

            try:
                with open(weightFile, "rt") as f:
                    try:
                        data = json.load(f)
                        useDefault = False
                    except json.JSONDecodeError:
                        useDefault = True
            except FileNotFoundError:
                useDefault = True

            if useDefault:
                data = {"weight": config['training_trace_selection_maximum_weight']}

            data['id'] = traceId
            data['executionSessionId'] = sessionId

            # getLogger().info(f"Loaded {traceId}")
            return pickle.dumps(data)
        except Exception as e:
            getLogger().error(traceback.format_exc())
            return None
