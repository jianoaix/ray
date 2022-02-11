#include <grpc/grpc.h>
#include <grpcpp/create_channel.h>
#include <grpcpp/security/credentials.h>
#include <grpcpp/security/server_credentials.h>
#include <grpcpp/server.h>
#include <grpcpp/server_builder.h>

#include <cstdlib>
#include <ctime>
#include <iostream>

#include "ray/common/asio/periodical_runner.h"
#include "ray/common/component_syncer.h"
#include "ray/common/id.h"
using namespace std;
using namespace ray::syncing;

class LocalNode : public Reporter {
 public:
  LocalNode(instrumented_io_context &io_context, ray::NodeID node_id)
      : node_id_(node_id), timer_(io_context) {
    timer_.RunFnPeriodically(
        [this]() {
          auto v = static_cast<double>(std::rand()) / RAND_MAX;
          if (v < 0.3) {
            int old_state = state_;
            state_ += std::rand() % 10;
            ++version_;
            RAY_LOG(INFO) << node_id_ << " change from (" << old_state
                          << ", v:" << (version_ - 1) << ") to (" << state_
                          << ", v:" << version_ << ")";
          }
        },
        1000);
  }

  std::optional<RaySyncMessage> Snapshot(uint64_t current_version) const override {
    if (current_version > version_) {
      return std::nullopt;
    }
    ray::rpc::syncer::RaySyncMessage msg;
    msg.set_message_type(ray::rpc::syncer::RaySyncMessageType::BROADCAST);
    msg.set_component_id(ray::rpc::syncer::RayComponentId::RESOURCE_MANAGER);
    msg.set_version(version_);
    msg.set_sync_message(
        std::string(reinterpret_cast<const char *>(&state_), sizeof(state_)));
    msg.set_node_id(node_id_.Binary());
    return msg;
  }

 private:
  uint64_t version_ = 1;
  int state_ = 0;
  ray::NodeID node_id_;
  ray::PeriodicalRunner timer_;
};

class RemoteNodes : public Receiver {
 public:
  RemoteNodes() {}
  void Update(const ray::rpc::syncer::RaySyncMessage &msg) override {
    int version = msg.version();
    int state = *reinterpret_cast<const int *>(msg.sync_message().data());
    auto iter = infos_.find(msg.node_id());
    if (iter == infos_.end() || iter->second.second < version) {
      RAY_LOG(INFO) << "Update node " << ray::NodeID::FromBinary(msg.node_id()).Hex()
                    << " to (" << state << ", v:" << version << ")";
      infos_[msg.node_id()] = std::make_pair(state, version);
    }
  }

 private:
  absl::flat_hash_map<std::string, std::pair<int, int>> infos_;
};

int main(int argc, char *argv[]) {
  std::srand(std::time(nullptr));
  instrumented_io_context io_context;
  RAY_CHECK(argc == 3) << "./test_syncer_service server_port leader_port";
  auto node_id = ray::NodeID::FromRandom();
  auto server_port = std::string(argv[1]);
  auto leader_port = std::string(argv[2]);
  auto local_node = std::make_unique<LocalNode>(io_context, node_id);
  auto remote_node = std::make_unique<RemoteNodes>();
  RaySyncer syncer(node_id.Binary(), io_context);
  // RPC related field
  grpc::ServerBuilder builder;
  std::unique_ptr<RaySyncerService> service;
  std::unique_ptr<grpc::Server> server;
  std::shared_ptr<grpc::Channel> channel;
  syncer.Register(ray::rpc::syncer::RayComponentId::RESOURCE_MANAGER, local_node.get(),
                  remote_node.get());
  if (server_port != ".") {
    RAY_LOG(INFO) << "Start server on port " << server_port;
    auto server_address = "0.0.0.0:" + server_port;
    service = std::make_unique<RaySyncerService>(syncer);
    builder.AddListeningPort(server_address, grpc::InsecureServerCredentials());
    builder.AddChannelArgument(GRPC_ARG_MAX_CONCURRENT_STREAMS, 2000);
    builder.AddChannelArgument(GRPC_ARG_HTTP2_WRITE_BUFFER_SIZE, 256 * 1024);
    builder.RegisterService(service.get());
    builder.AddCompletionQueue();
    server = builder.BuildAndStart();
  }
  if (leader_port != ".") {
    grpc::ChannelArguments argument;
    // Disable http proxy since it disrupts local connections. TODO(ekl) we should make
    // this configurable, or selectively set it for known local connections only.
    argument.SetInt(GRPC_ARG_ENABLE_HTTP_PROXY, 0);
    argument.SetMaxSendMessageSize(::RayConfig::instance().max_grpc_message_size());
    argument.SetMaxReceiveMessageSize(::RayConfig::instance().max_grpc_message_size());

    channel = grpc::CreateCustomChannel("localhost:" + leader_port,
                                        grpc::InsecureChannelCredentials(), argument);
    syncer.ConnectTo(ray::rpc::syncer::RaySyncer::NewStub(channel));
  }
  boost::asio::io_context::work work(io_context);
  io_context.run();

  return 0;
}
